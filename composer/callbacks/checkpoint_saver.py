# Copyright 2022 MosaicML Composer authors
# SPDX-License-Identifier: Apache-2.0

"""Callback to save checkpoints during training."""

from __future__ import annotations

import logging
import math
import os
import pathlib
import shutil
import tempfile
import textwrap
from pathlib import Path
from typing import Callable, List, Optional, Union

from composer.core import Callback, Event, State, Time, TimeUnit
from composer.loggers import Logger
from composer.utils import (FORMAT_NAME_WITH_DIST_AND_TIME_TABLE, FORMAT_NAME_WITH_DIST_TABLE, PartialFilePath,
                            checkpoint, create_symlink_file, dist, ensure_folder_has_no_conflicting_files,
                            format_name_with_dist, format_name_with_dist_and_time, is_model_deepspeed, reproducibility)
from composer.utils.checkpoint import _TORCH_DISTRIBUTED_CHECKPOINTS_FILENAME
from composer.utils.misc import using_torch_2

log = logging.getLogger(__name__)

__all__ = ['CheckpointSaver', 'checkpoint_periodically']

_TORCH_DISTRIBUTED_CHECKPOINTS_METADATA_FILENAME = '.metadata'


def checkpoint_periodically(interval: Union[str, int, Time]) -> Callable[[State, Event], bool]:
    r"""Helper function to create a checkpoint scheduler according to a specified interval.

    Args:
        interval (Union[str, int, :class:`.Time`]): The interval describing how often checkpoints should be
            saved. If an integer, it will be assumed to be in :attr:`.TimeUnit.EPOCH`\s.
            Otherwise, the unit must be either :attr:`.TimeUnit.EPOCH`, :attr:`.TimeUnit.BATCH`,
            :attr:`.TimeUnit.TOKEN`, or :attr:`.TimeUnit.SAMPLE`.

            Checkpoints will be saved every ``n`` batches or epochs (depending on the unit),
            and at the end of training.

    Returns:
        Callable[[State, Event], bool]: A function that can be passed as the ``save_interval``
            argument into the :class:`.CheckpointSaver`.
    """
    if isinstance(interval, str):
        interval = Time.from_timestring(interval)
    if isinstance(interval, int):
        interval = Time(interval, TimeUnit.EPOCH)

    if interval.unit == TimeUnit.EPOCH:
        save_event = Event.EPOCH_CHECKPOINT
    elif interval.unit in {TimeUnit.BATCH, TimeUnit.TOKEN, TimeUnit.SAMPLE}:
        save_event = Event.BATCH_CHECKPOINT
    else:
        raise NotImplementedError(
            f'Unknown checkpointing interval: {interval.unit}. Must be TimeUnit.EPOCH, TimeUnit.BATCH, TimeUnit.TOKEN, or TimeUnit.SAMPLE.'
        )

    def save_interval(state: State, event: Event):
        elapsed_duration = state.get_elapsed_duration()
        assert elapsed_duration is not None, 'elapsed_duration is set on the BATCH_CHECKPOINT and EPOCH_CHECKPOINT'

        # Always checkpoint at end of training
        if elapsed_duration >= 1.0:
            return True

        # previous timestamp will only be None if training has not started, but we are returning False
        # in this case, just to be safe
        if state.previous_timestamp is None:
            return False

        if interval.unit in {TimeUnit.EPOCH, TimeUnit.BATCH, TimeUnit.TOKEN, TimeUnit.SAMPLE}:
            previous_count = state.previous_timestamp.get(interval.unit)
            count = state.timestamp.get(interval.unit)
        else:
            raise NotImplementedError(
                f'Unknown checkpointing interval: {interval.unit}. Must be TimeUnit.EPOCH, TimeUnit.BATCH, TimeUnit.TOKEN, or TimeUnit.SAMPLE.'
            )

        threshold_passed = math.floor(previous_count / interval.value) != math.floor(count / interval.value)
        return event == save_event and threshold_passed

    return save_interval


class CheckpointSaver(Callback):  # noqa: D101
    __doc__ = f"""Callback to save checkpoints.

    .. note::

        If the ``folder`` argument is specified when constructing the :class:`.Trainer`, then the :class:`.CheckpointSaver`
        callback need not be constructed manually. However, for advanced checkpointing use cases
        (such as saving a weights-only checkpoint at one interval and the full training state
        at another interval), instance(s) of this :class:`.CheckpointSaver` callback can be specified in the
        ``callbacks`` argument of the :class:`.Trainer`, as shown in the example below.

    Example

    .. testsetup::

        from composer.callbacks.checkpoint_saver import CheckpointSaver

    .. doctest::

        >>> trainer = Trainer(..., callbacks=[
        ...     CheckpointSaver(
        ...         folder='{{run_name}}/checkpoints',
        ...         filename="ep{{epoch}}-ba{{batch}}-rank{{rank}}",
        ...         latest_filename="latest-rank{{rank}}",
        ...         save_interval="1ep",
        ...         weights_only=False,
        ...     )
        ... ])

    Args:
        folder (str, optional): Format string for the save_folder where checkpoints will be saved.
            Default: ``'{{run_name}}/checkpoints'``.

            The following format variables are available:

            {textwrap.indent(FORMAT_NAME_WITH_DIST_TABLE, prefix='            ')}

            .. note::

                When training with multiple devices (i.e. GPUs), ensure that ``'{{rank}}'`` appears in the format.
                Otherwise, multiple processes may attempt to write to the same file.

        filename (str, optional): A format string describing how to name checkpoints.
            Default: ``'ep{{epoch}}-ba{{batch}}-rank{{rank}}.pt'``.

            Checkpoints will be saved approximately to ``{{folder}}/{{filename.format(...)}}``.

            The following format variables are available:

            {textwrap.indent(FORMAT_NAME_WITH_DIST_AND_TIME_TABLE, prefix='            ')}


            .. note::

                *   By default, only the rank zero process will save a checkpoint file.

                *   When using DeepSpeed, each rank will save a checkpoint file in tarball format. DeepSpeed
                    requires tarball format, as it saves model and optimizer states in separate files.
                    Ensure that ``'{{rank}}'`` appears within the ``filename``. Otherwise, multiple ranks
                    may attempt to write to the same file(s), leading to corrupted checkpoints. If no tarball file
                    extension is specified, ``'.tar'`` will be used.

                *   To use compression (regardless of whether DeepSpeed is enabled), set the file extension
                    to ``'.tar.gz'``, ``'.tgz'``, ``'.tar.bzip'``, or ``'.tar.lzma'`` (depending on the desired
                    compression algorithm).

            .. warning::

                Using compression will block the training loop while checkpoints are being compressed. As such, we
                recommend saving checkpoints without compression.

            Consider the following scenario where:

            *   The :attr:`~.State.run_name` is ``'awesome-training-run'``
            *   The default ``folder='{{run_name}}/checkpoints'`` is used.
            *   The default ``name='ep{{epoch}}-ba{{batch}}-rank{{rank}}'`` is used.
            *   The current epoch count is ``1``.
            *   The current batch count is ``42``.

            When DeepSpeed is not being used, the rank zero process will save the checkpoint to
            ``"awesome-training-run/checkpoints/ep1-ba42-rank0"``.

            When DeepSpeed is being used, each rank (process) will save checkpoints to::

                awesome-training-run/checkpoints/ep1-ba42-rank0.tar
                awesome-training-run/checkpoints/ep1-ba42-rank1.tar
                awesome-training-run/checkpoints/ep1-ba42-rank2.tar
                ...

        remote_file_name (str, optional): Format string for the checkpoint's remote file name.
            Default: ``"{{run_name}}/checkpoints/ep{{epoch}}-ba{{batch}}-rank{{rank}}"``.

            After the checkpoint is saved, it will be periodically uploaded.
            The remote file name will be determined by this format string.

            .. seealso:: :doc:`Uploading Files</trainer/file_uploading>` for notes for file uploading.

            The same format variables for ``filename`` are available.

            Leading slashes (``'/'``) will be stripped.

            To disable uploading checkpoints, set this parameter to ``None``.
        latest_filename (str, optional): A format string for a symlink which points to the last saved checkpoint.
            Default: ``'latest-rank{{rank}}.pt'``.

            Symlinks will be created approximately at ``{{folder}}/{{latest_filename.format(...)}}``.

            The same format variables as for ``name`` are available.

            To disable symlinks, set this parameter to ``None``.

            Consider the following scenario, where:

            *   The :attr:`~.State.run_name` is 'awesome-training-run'
            *   The default ``folder='{{run_name}}/checkpoints'`` is used.
            *   The default ``name='ep{{epoch}}-ba{{batch}}-rank{{rank}}'`` is used.
            *   The default ``latest_filename='latest-rank{{rank}}'`` is used.
            *   The current epoch count is ``1``.
            *   The current batch count is ``42``.

            When DeepSpeed is not being used, the rank zero process will save the checkpoint to
            ``'awesome-training-run/checkpoints/ep1-ba42-rank0'``,
            and a symlink will be created at
            ``'awesome-training-run/checkpoints/latest-rank0' -> 'awesome-training-run/checkpoints/ep1-ba42-rank0'``

            When DeepSpeed is being used, each rank (process) will save checkpoints to::

                awesome-training-run/checkpoints/ep1-ba42-rank0.tar
                awesome-training-run/checkpoints/ep1-ba42-rank1.tar
                awesome-training-run/checkpoints/ep1-ba42-rank2.tar
                ...

            Corresponding symlinks will be created at::

                awesome-training-run/checkpoints/latest-rank0.tar -> awesome-training-run/checkpoints/ep1-ba42-rank0.tar
                awesome-training-run/checkpoints/latest-rank1.tar -> awesome-training-run/checkpoints/ep1-ba42-rank1.tar
                awesome-training-run/checkpoints/latest-rank2.tar -> awesome-training-run/checkpoints/ep1-ba42-rank2.tar
                ...
        latest_remote_file_name (str, optional): Format string for the checkpoint's latest symlink remote file name.
            Default: ``'{{run_name}}/checkpoints/latest-rank{{rank}}"``.

            Whenever a new checkpoint is saved, a symlink is created or updated to point to the latest checkpoint's ``remote_file_name``.
            The remote file name will be determined by this format string. This parameter has no effect if ``latest_filename`` or ``remote_file_name`` is ``None``.

            .. seealso:: :doc:`Uploading Files</trainer/file_uploading>` for notes for file uploading.

            The same format variables for ``filename`` are available.

            Leading slashes (``'/'``) will be stripped.

            To disable symlinks in logger, set this parameter to ``None``.

        overwrite (bool, optional): Whether existing checkpoints should be overridden.
            If ``False`` (the default), then the ``folder`` must not exist or must not contain checkpoints which may conflict
            with the current run. Default: ``False``.

        save_interval (Time | str | int | (State, Event) -> bool): A :class:`.Time`, time-string, integer (in epochs),
            or a function that takes (state, event) and returns a boolean whether a checkpoint should be saved.

            If an integer, checkpoints will be saved every n epochs.
            If :class:`.Time` or a time-string, checkpoints will be saved according to this interval.

            .. seealso:: :func:`.checkpoint_periodically`

            If a function, then this function should take two arguments (:class:`.State`, :class:`.Event`).
            The first argument will be the current state of the trainer, and the second argument will be
            be :attr:`.Event.BATCH_CHECKPOINT` or :attr:`.Event.EPOCH_CHECKPOINT` (depending on the current training
            progress). It should return ``True`` if a checkpoint should be saved given the current state and
            event.

        weights_only (bool): If ``True``, save only the model weights instead of the entire training state.
            This parameter must be ``False`` when using DeepSpeed. Default: ``False``.


        num_checkpoints_to_keep (int, optional): The number of checkpoints to keep locally. The oldest checkpoints
            are removed first. Set to ``-1`` to keep all checkpoints locally. Default: ``-1``.

            Checkpoints will be removed after they have been uploaded. For example, when this callback
            is used in conjunction with the :class:`.RemoteUploaderDownloader`, set this
            parameter to ``0`` to immediately delete checkpoints from the local disk after they have been uploaded to
            the object store.

            This parameter only controls how many checkpoints are kept locally; checkpoints are not deleted from
            remote file systems.

    Attributes:
        saved_checkpoints (List[Tuple[Timestamp, List[pathlib.Path]]]): The checkpoint timestamps and filepaths.

            This list contains tuples of the save timestamp and the checkpoint filepaths.
            This list will have at most ``num_checkpoints_to_keep`` entries. The latest checkpoint
            will be at the end.

            .. note::

                When using DeepSpeed, the index of a filepath in each list corresponds to the global rank of
                the process that wrote that file. Each filepath is valid only on the process's (rank's) node.

                Otherwise, when not using DeepSpeed, each sub-list will contain only one filepath since only rank zero
                saves checkpoints.
    """

    def __init__(
        self,
        folder: Union[str, pathlib.Path] = '{run_name}/checkpoints',
        filename: Union[str, pathlib.Path] = 'ep{epoch}-ba{batch}-rank{rank}.pt',
        remote_file_name: Optional[Union[str,
                                         pathlib.Path]] = '{run_name}/checkpoints/ep{epoch}-ba{batch}-rank{rank}.pt',
        latest_filename: Optional[Union[str, pathlib.Path]] = 'latest-rank{rank}.pt',
        latest_remote_file_name: Optional[Union[str, pathlib.Path]] = '{run_name}/checkpoints/latest-rank{rank}.pt',
        save_interval: Union[Time, str, int, Callable[[State, Event], bool]] = '1ep',
        *,
        overwrite: bool = False,
        num_checkpoints_to_keep: int = -1,
        weights_only: bool = False,
    ):
        folder = str(folder)
        filename = str(filename)
        remote_file_name = str(remote_file_name) if remote_file_name is not None else None
        latest_filename = str(latest_filename) if latest_filename is not None else None
        latest_remote_file_name = str(latest_remote_file_name) if latest_remote_file_name is not None else None

        if not callable(save_interval):
            save_interval = checkpoint_periodically(save_interval)
        self.save_interval = save_interval
        self.last_checkpoint_batch: Optional[Time] = None

        self.folder = folder

        self.filename = PartialFilePath(filename.lstrip('/'), folder)
        self.latest_filename = PartialFilePath(latest_filename.lstrip('/'), folder) if latest_filename else None
        self.remote_file_name = PartialFilePath(remote_file_name) if remote_file_name else None
        self.latest_remote_file_name = PartialFilePath(latest_remote_file_name) if latest_remote_file_name else None

        self.overwrite = overwrite
        self.saved_checkpoints: List[str] = []
        self.num_checkpoints_to_keep = num_checkpoints_to_keep
        self.weights_only = weights_only

        self.start_batch = None

    def init(self, state: State, logger: Logger) -> None:
        folder = format_name_with_dist(self.folder, state.run_name)
        os.makedirs(folder, exist_ok=True)

    def fit_start(self, state: State, logger: Logger) -> None:
        if not self.overwrite:
            # checks that save_folder contains no files with a timestamp after the current timestamp,
            # which has potential for future conflicts.
            folder = format_name_with_dist(self.folder, state.run_name)
            ensure_folder_has_no_conflicting_files(folder, self.filename.filename, state.timestamp)

        dist.barrier()  # holds all ranks until folder check is done

        if is_model_deepspeed(state.model) and self.weights_only:
            raise NotImplementedError('weights_only=True is not supported when using DeepSpeed.')

        self.start_batch = state.timestamp.batch

    def batch_checkpoint(self, state: State, logger: Logger):
        assert callable(self.save_interval)
        if self.save_interval(state, Event.BATCH_CHECKPOINT) and self.last_checkpoint_batch != state.timestamp.batch:
            self._save_checkpoint(
                state,
                logger,
            )

    def epoch_checkpoint(self, state: State, logger: Logger):
        assert callable(self.save_interval)
        if self.save_interval(state, Event.EPOCH_CHECKPOINT) and self.last_checkpoint_batch != state.timestamp.batch:
            self._save_checkpoint(
                state,
                logger,
            )

    def close(self, state: State, logger: Logger):
        trained_at_least_one_batch = self.start_batch is not None and self.start_batch != state.timestamp.batch
        if self.last_checkpoint_batch != state.timestamp.batch and trained_at_least_one_batch:
            self._save_checkpoint(
                state,
                logger,
            )

    def get_state_dict(self, state):
        return {
            'state': state.state_dict(),
            'rng': reproducibility.get_rng_state(),
        }

    def _save_checkpoint(self, state: State, logger: Logger):
        self.last_checkpoint_batch = state.timestamp.batch

        is_deepspeed = is_model_deepspeed(state.model)

        if is_deepspeed and '{rank}' not in self.filename.filename:
            raise ValueError(f'Save filename {self.filename.filename} must have {{rank}} for deepspeed.')

        # save the checkpoint to the filename
        filename_with_placeholders = self.filename.format(state, is_deepspeed, keep_placeholders=True)

        saved_path = checkpoint.save_checkpoint(
            state=state,
            filename=filename_with_placeholders,
            weights_only=self.weights_only,
        )
        log.debug(f'Checkpoint locally saved to {saved_path}')

        if not saved_path:  # not all ranks save
            return
        metadata_local_file_path = None
        if dist.get_global_rank() == 0 and state.fsdp_elastic_sharded_enabled:
            metadata_local_file_path = format_name_with_dist_and_time(
                os.path.join(Path(saved_path).parent, _TORCH_DISTRIBUTED_CHECKPOINTS_METADATA_FILENAME), state.run_name,
                state.timestamp)

        if self.latest_filename is not None and self.num_checkpoints_to_keep != 0:
            symlink = self.latest_filename.format(state, is_deepspeed)
            os.makedirs(os.path.dirname(symlink), exist_ok=True)
            try:
                os.remove(symlink)
            except FileNotFoundError:
                pass
            # Sharded checkpoints for torch >2.0 use directories not files for load_paths
            if state.fsdp_elastic_sharded_enabled:
                src_path = str(pathlib.Path(saved_path).parent)
            else:
                src_path = saved_path
            this_rank_saves_symlinks = dist.get_global_rank() == 0 or not state.fsdp_elastic_sharded_enabled
            if this_rank_saves_symlinks:
                os.symlink(os.path.relpath(src_path, os.path.dirname(symlink)), symlink)

        # if remote file name provided, upload the checkpoint
        if self.remote_file_name is not None:
            if state.fsdp_sharded_state_dict_enabled:
                remote_file_name = self.remote_file_name.format(
                    state,
                    is_deepspeed,
                    keep_placeholders=True,
                ).lstrip('/')
                assert state.sharded_ckpt_prefix_dir is not None
                remote_prefix = state.sharded_ckpt_prefix_dir
                ckpt_filename = _TORCH_DISTRIBUTED_CHECKPOINTS_FILENAME if using_torch_2() else pathlib.Path(
                    remote_file_name).name
                remote_file_name = os.path.join(pathlib.Path(remote_file_name).parent, remote_prefix, ckpt_filename)
                remote_file_name = format_name_with_dist_and_time(remote_file_name, state.run_name, state.timestamp)
                # Upload metadata file.
                # The metadata file contains info related to which shards are saved where.
                if dist.get_global_rank() == 0 and state.fsdp_elastic_sharded_enabled:
                    metadata_remote_file_name = format_name_with_dist_and_time(
                        os.path.join(Path(remote_file_name).parent, _TORCH_DISTRIBUTED_CHECKPOINTS_METADATA_FILENAME),
                        state.run_name, state.timestamp)
                    assert metadata_local_file_path is not None
                    logger.upload_file(remote_file_name=metadata_remote_file_name,
                                       file_path=metadata_local_file_path,
                                       overwrite=self.overwrite)
            else:
                remote_file_name = self.remote_file_name.format(
                    state,
                    is_deepspeed,
                ).lstrip('/')

            log.debug(f'Uploading checkpoint to {remote_file_name}')
            logger.upload_file(remote_file_name=remote_file_name, file_path=saved_path, overwrite=self.overwrite)

            # symlinks stay the same with sharded checkpointing
            if self.latest_remote_file_name is not None:
                symlink_name = self.latest_remote_file_name.format(
                    state,
                    is_deepspeed,
                ).lstrip('/') + '.symlink'

                # create and upload a symlink file
                with tempfile.TemporaryDirectory() as tmpdir:
                    symlink_filename = os.path.join(tmpdir, 'latest.symlink')
                    # Sharded checkpoints for torch >2.0 use directories not files for load_paths
                    if state.fsdp_elastic_sharded_enabled:
                        src_path = str(pathlib.Path(remote_file_name).parent)
                    else:
                        src_path = remote_file_name
                    log.debug(f'Creating symlink file {symlink_filename} -> {src_path}')
                    this_rank_saves_symlinks = dist.get_global_rank() == 0 or not state.fsdp_elastic_sharded_enabled
                    if this_rank_saves_symlinks:
                        create_symlink_file(src_path, symlink_filename)
                        logger.upload_file(
                            remote_file_name=symlink_name,
                            file_path=symlink_filename,
                            overwrite=True,
                        )

        self.saved_checkpoints.append(saved_path)

        if self.num_checkpoints_to_keep >= 0:
            self._rotate_checkpoints(sharding_enabled=state.fsdp_sharded_state_dict_enabled)

    def _rotate_checkpoints(self, sharding_enabled: bool = False):

        while len(self.saved_checkpoints) > self.num_checkpoints_to_keep:
            prefix_dir = None
            checkpoint = self.saved_checkpoints.pop(0)
            prefix_dir = str(Path(checkpoint).parent)
            if not sharding_enabled:
                os.remove(checkpoint)
            else:
                if dist.get_global_rank() == 0:
                    shutil.rmtree(prefix_dir)
