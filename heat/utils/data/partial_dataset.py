"""
Tool for using a dataset which will not fit in memory with neural networks
"""

import math
import queue
import threading
import torch
import time

from torch.utils import data as torch_data
from typing import Callable, List, Iterator, Union

from ...core.communication import MPICommunication
from ...core.communication import MPI_WORLD

__all__ = ["PartialH5Dataset", "PartialH5DataLoaderIter"]


def queue_thread(q: queue.Queue):
    while True:
        items = q.get()
        if isinstance(items, tuple):
            func = items[0]
            args = items[1:]
            func(*args)
        else:
            items()
        q.task_done()


class PartialH5Dataset(torch_data.Dataset):
    """
    Create a Dataset object for a dataset which loads portions of data from an HDF5 file. Very similar to
    :func:`<heat.utils.data.datatools.Dataset>`. This will create 2 threads, one for loading the data from the target file,
    and one for converting items before being passed to the network. The conversion is done by the iterator.
    A portion of the data of length ``initial_load`` is loaded upon initialization, the rest of the data is loaded
    after the loaded data is returned by :func:`PartialH5DataLoaderIter`. This iterator will be used by the HeAT
    :func:`heat.utils.data.datatools.DataLoader` automatically with this type of dataset.

    Notes
    -----
    H5 datasets require the GIL to load data. This can be a bottleneck if data needs to be loaded multiple times (e.g.
    the case for using this dataset). It is recommended to find another way to preprocess the data and avoid using
    H5 files for this reason.

    Parameters
    ----------
    file: str
        H5 file to use
    comm: MPICommunication
        Global MPI communicator generated by HeAT
    dataset_names: Union[str, List[str]], optional
        Name/s of dataset/s to load from ``file``. If a string is given, it will be the only dataset loaded.
        Default is "data".
    transforms : List[Callable], optional
        Transforms to apply to the data after it is gotten from the loaded data before it is used by the network.
        This should be a list of Callable torch functions for each item returned by the ``__getitem__`` function
        of the individual dataset. If a list element is ``None`` then no transform will be applied to the
        corresponding element returned by ``__getitem__``. I.e. if ``__getitem__`` returns an image and a label
        then the list would look like this: ``transforms = [image_transforms, None]``. If this is ``None``, no
        transforms will be applied to any elements. Default is ``None``.
    use_gpu : bool, optional
        Use GPUs if available. Defaults to True.
    validate_set : bool, optional
        Load the entire dataset onto each node upon initialization and skip loaded in iterator
        This is typically the case needed for validation sets when the network should be tested against the whole
        dataset. Default is False.
    initial_load : int, optional
        How many elements to load from the file in the 0th dimension. Default is 7000 elements
    load_length : int, optional
        How many elements to load from the file in the iterator. Default is 1000 elements
    """

    def __init__(
        self,
        file: str,
        comm: MPICommunication = MPI_WORLD,
        dataset_names: Union[str, List[str]] = "data",
        transforms: List[Callable] = None,
        use_gpu: bool = True,
        validate_set: bool = False,
        initial_load: int = 7000,
        load_length: int = 1000,
    ):  # noqa: D107
        import h5py

        super(PartialH5Dataset, self).__init__()
        self.ishuffle = False
        self.file = file
        self.comm = comm
        self.transforms = transforms if isinstance(transforms, (list, tuple)) else [transforms]
        self.gpu = True if torch.cuda.device_count() > 0 and use_gpu else False
        self.torch_device = "cpu"
        if torch.cuda.is_available() and use_gpu:
            dev_id = MPI_WORLD.rank % torch.cuda.device_count()
            self.torch_device = torch.device("cuda:" + str(dev_id))
            torch.cuda.set_device(dev_id)

        f = h5py.File(file, "r")
        # too much data for the process
        fkeys = list(f.keys())

        sz = f[fkeys[0]].len()
        for k in fkeys[1:]:
            # ensure that all of the datasets are the same length
            if f[k].len() != sz:
                raise ValueError(f"all datasets in {file} must be the same length")
        self.total_size = sz
        # how many indices will go onto each process (len)
        self.lcl_full_sz = sz // comm.size
        # load data that is half of of the available memory
        self.local_data_start = comm.rank * self.lcl_full_sz
        self.local_data_end = (comm.rank + 1) * self.lcl_full_sz

        if validate_set or initial_load > self.lcl_full_sz:
            # if its the validation set then load the whole dataset for each process
            self.lcl_full_sz = sz
            self.local_data_start = 0
            self.local_data_end = sz
            self.load_initial = sz
            self.partial_dataset = False
            self.load_len = 0
            self.loads_needed = 0
        else:
            self.local_length = self.local_data_end - self.local_data_start
            self.load_initial = initial_load
            self.load_len = load_length  # int(local_data_end / 3)
            self.loads_needed = math.ceil(self.lcl_full_sz / self.load_len)
            self.partial_dataset = True

        self.loads_left = self.loads_needed
        self.load_start = self.local_data_start
        self.load_end = self.local_data_start + self.load_initial

        # data being loaded from dataset_names parameter
        if isinstance(dataset_names, str):
            dataset_names = [dataset_names]
        self.dataset_names = dataset_names
        self.dataset_order = []
        for d in dataset_names:
            hld = f[d][self.load_start : self.load_end]
            self.__setattr__(d, hld)
        self.load_start = self.load_end
        self.load_end += self.load_len
        f.close()
        self.load_thread = None
        self.epoch_end = False
        # need the number of loads required for an epoch
        self.loading_queue = queue.Queue()
        self.loading_condition = threading.Condition()
        threading.Thread(target=queue_thread, args=[self.loading_queue], daemon=True).start()
        self.convert_queue = queue.Queue()
        threading.Thread(target=queue_thread, args=[self.convert_queue], daemon=True).start()
        self.used_indices = []

    def Shuffle(self):
        """
        Send half of the local data to the process ``self.comm.rank + 1`` if available, else wrap around. After
        receiving the new data, shuffle the local tensor.

        Not implemented for partial dataset
        """
        return NotImplementedError

    def Ishuffle(self):
        """
        Send half of the local data to the process ``self.comm.rank + 1`` if available, else wrap around. After
        receiving the new data, shuffle the local tensor.

        Not implemented for partial dataset
        """
        return NotImplementedError

    def __getitem__(self, index: Union[int, slice, List[int], torch.Tensor]) -> torch.Tensor:
        """
        This should be defined by the user at runtime. This function needs to be designed such
        that the data is in the 0th dimension and the indexes called are only in the 0th dim!
        """
        raise NotImplementedError("__getitem__ must be overwritten")

    def __len__(self) -> int:
        """
        Get the total length of the dataset
        """
        return self.total_size

    def thread_replace_converted_batches(self):
        """
        Replace the elements of the dataset with newly loaded elements. :func:'PartialH5DataLoaderIter' will
        put the used indices in the ``used_indices`` parameter. This object is reset to an empty list after
        these elements are overwritten with new data.
        """
        import h5py

        self.loads_left = self.loads_needed
        ll = self.loads_left
        for _ in range(ll):
            with h5py.File(self.file, "r") as f:
                for d in self.dataset_names:
                    hld = f[d][self.load_start : self.load_end]
                    self.__setattr__("hold" + d, hld)
            if self.load_end + self.comm.size > self.total_size:
                self.load_end = 0
            self.load_start = self.load_end
            self.load_end += self.load_len

            # wait for lock1 *from* convert thread
            with self.loading_condition:
                self.loading_condition.wait()
                for d in self.dataset_names:
                    new = self.__getattribute__("hold" + d)
                    dset = self.__getattribute__(d)
                    new_top = new[: len(self.used_indices)]
                    lnew = len(new_top)
                    dset[self.used_indices[:lnew]] = new_top
                    self.__setattr__(d, dset)
                    self.__setattr__("hold" + d, new[lnew:])
                # give up lock / notify convert thread
                self.used_indices = []
            self.loads_left -= 1


class PartialH5DataLoaderIter(object):
    """
    Iterator to be used with :func:'PartialH5Dataset'. It closely mirrors the standard torch iterator while loading
    new data to replace the loaded batches automatically. It also pre-fetches the batches and begins their
    preparation, collation, and device setting in the background.
    """

    def __init__(self, loader):  # noqa: D107
        # todo: make note that h5py is required for this...move load to dataset?
        self.dataset = loader.dataset
        self._dataset_kind = loader.DataLoader._dataset_kind
        self._IterableDataset_len_called = loader.DataLoader._IterableDataset_len_called
        self._auto_collation = loader.DataLoader._auto_collation
        self._drop_last = loader.DataLoader.drop_last
        self._index_sampler = loader.DataLoader._index_sampler
        self._num_workers = loader.DataLoader.num_workers
        self._pin_memory = loader.DataLoader.pin_memory and torch.cuda.is_available()
        self._timeout = loader.DataLoader.timeout
        self._collate_fn = loader.DataLoader.collate_fn
        self._sampler_iter = iter(self._index_sampler)
        self._base_seed = torch.empty((), dtype=torch.int64).random_().item()
        self._num_yielded = 0
        self.batch_size = loader.DataLoader.batch_size
        self.comm = self.dataset.comm
        rand_samp_list = torch.randperm(self.dataset.load_initial).tolist()

        # todo: support other samplers: for now its only random
        if self.dataset.partial_dataset:
            self.ready_batches = []
            mod_batch = self.dataset.load_len % self.batch_size
            if mod_batch != 0:
                self.dataset.load_len += self.batch_size - mod_batch
                self.dataset.load_end = self.dataset.load_start + self.dataset.load_len
            # generate all indices
            index_list = []
            idx_repeats = math.ceil(self.dataset.lcl_full_sz / self.dataset.load_initial)
            for _ in range(idx_repeats):
                index_list.extend(torch.randperm(self.dataset.load_initial).tolist())
            # start the conversion
            self.dataset.convert_queue.put((self.__thread_convert_all, index_list))

            self.length = len(index_list) // self.batch_size
            self.dataset.loading_queue.put(self.dataset.thread_replace_converted_batches)
        else:
            self.rand_samp_list = rand_samp_list
            self.length = len(self._index_sampler)

        self._dataset_fetcher = torch_data.dataloader._DatasetKind.create_fetcher(
            self._dataset_kind,
            loader.DataLoader.dataset,
            self._auto_collation,
            self._collate_fn,
            self._drop_last,
        )

    def __len__(self):
        """
        Get the length of the iterator
        """
        return self.length

    def _next_data(self):
        # get the next batch
        if not self.dataset.partial_dataset:
            index = next(self._sampler_iter)  # may raise StopIteration
            data = self._dataset_fetcher.fetch(index)  # may raise StopIteration
            if self._pin_memory:
                data = torch_data._utils.pin_memory.pin_memory(data)
            return data
        if self._num_yielded == self.__len__():
            raise StopIteration
        while len(self.ready_batches) < 1:
            time.sleep(0.1)
        batch = self.ready_batches.pop(0)
        for b in range(len(batch)):
            if batch[b].device != self.dataset.torch_device:
                batch[b] = batch[b].to(self.dataset.torch_device)
        return batch

    def __next__(self):
        """
        Get the next batch of data. Shamelessly taken from torch.
        """
        # shamelessly taken from torch
        data = self._next_data()
        self._num_yielded += 1
        # note: the warnings raised by torch for iterable datasets were removed here, look for these in
        #       the base class of the single process iterator
        return data

    def __iter__(self):
        """
        Get a new iterator of this class

        Returns
        -------
        PartialH5DataLoaderIter
        """
        return self

    def __thread_convert_all(self, index_list):
        # convert all of the elements, collate them into batches, and send the batches to the correct device
        # this function als communicates with the data loading thread from the PartialH5Dataset to notify it
        # when it has the correct amount of data to write.
        converted_items = []
        for ind in index_list:
            # get the desired image/target/... to begin composing a batch
            single_item = self.dataset[ind]
            if not isinstance(single_item, tuple) and self.dataset.transforms[0] is not None:
                single_item = self.dataset.transforms[0](single_item)
            if isinstance(single_item, tuple):
                single_item = list(single_item)
                for ii in range(len(single_item)):
                    # do transforms (have all torch stuff here)
                    if self.dataset.transforms[ii] is not None:
                        single_item[ii] = self.dataset.transforms[ii](single_item[ii])
            converted_items.append(single_item)
            self.dataset.used_indices.append(ind)
            if len(converted_items) == self.batch_size:
                if (
                    len(self.dataset.used_indices) == self.dataset.load_len
                    and self.dataset.loads_left > 0
                ):
                    with self.dataset.loading_condition:
                        self.dataset.loading_condition.notify()
                batch = self._collate_fn(converted_items)
                try:
                    for bb in range(2):
                        bb_batch = self.ready_batches[bb]
                        for b in range(len(batch)):
                            bb_batch[b] = bb_batch[b].to(self.dataset.torch_device)
                        self.ready_batches[bb] = bb_batch
                except IndexError:
                    pass
                self.ready_batches.append(batch)
                converted_items = []
