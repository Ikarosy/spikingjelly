from torchvision.datasets import DatasetFolder
from typing import Any, Callable, cast, Dict, List, Optional, Tuple
from abc import abstractmethod
import scipy.io
import struct
import numpy as np
from torchvision.datasets import utils
import torch.utils.data
import os
from concurrent.futures import ThreadPoolExecutor
import time
import multiprocessing
from torchvision import transforms
import torch
from matplotlib import pyplot as plt
import math

def play_frame(x: torch.Tensor or np.ndarray, save_gif_to: str = None) -> None:
    '''
    :param x: frames with ``shape=[T, 2, H, W]``
    :type x: torch.Tensor or np.ndarray
    :param save_gif_to: If ``None``, this function will play the frames. If ``True``, this function will not play the frames
        but save frames to a gif file in the directory ``save_gif_to``
    :type save_gif_to: str
    :return: None
    '''
    if isinstance(x, np.ndarray):
        x = torch.from_numpy(x)
    to_img = transforms.ToPILImage()
    img_tensor = torch.zeros([x.shape[0], 3, x.shape[2], x.shape[3]])
    img_tensor[:, 1] = x[:, 0]
    img_tensor[:, 2] = x[:, 1]
    if save_gif_to is None:
        while True:
            for t in range(img_tensor.shape[0]):
                    plt.imshow(to_img(img_tensor[t]))
                    plt.pause(0.01)
    else:
        img_list = []
        for t in range(img_tensor.shape[0]):
            img_list.append(to_img(img_tensor[t]))
        img_list[0].save(save_gif_to, save_all=True, append_images=img_list[1:])
        print(f'Save frames to [{save_gif_to}].')


def load_matlab_mat(file_name: str) -> Dict:
    '''
    :param file_name: path of the matlab's mat file
    :type file_name: str
    :return: a dict whose keys are ['t', 'x', 'y', 'p'] and values are ``numpy.ndarray``
    :rtype: Dict
    '''
    events = scipy.io.loadmat(file_name)
    return {
        't': events['ts'].squeeze(),
        'x': events['x'].squeeze(),
        'y': events['y'].squeeze(),
        'p': events['pol'].squeeze()
    }


def load_aedat_v3(file_name: str) -> Dict:
    '''
    :param file_name: path of the aedat v3 file
    :type file_name: str
    :return: a dict whose keys are ['t', 'x', 'y', 'p'] and values are ``numpy.ndarray``
    :rtype: Dict

    This function is written by referring to https://gitlab.com/inivation/dv/dv-python . It can be used for DVS128 Gesture.
    '''
    with open(file_name, 'rb') as bin_f:
        # skip ascii header
        line = bin_f.readline()
        while line.startswith(b'#'):
            if line == b'#!END-HEADER\r\n':
                break
            else:
                line = bin_f.readline()

        txyp = {
            't': [],
            'x': [],
            'y': [],
            'p': []
        }
        while True:
            header = bin_f.read(28)
            if not header or len(header) == 0:
                break

            # read header
            e_type = struct.unpack('H', header[0:2])[0]
            e_source = struct.unpack('H', header[2:4])[0]
            e_size = struct.unpack('I', header[4:8])[0]
            e_offset = struct.unpack('I', header[8:12])[0]
            e_tsoverflow = struct.unpack('I', header[12:16])[0]
            e_capacity = struct.unpack('I', header[16:20])[0]
            e_number = struct.unpack('I', header[20:24])[0]
            e_valid = struct.unpack('I', header[24:28])[0]

            data_length = e_capacity * e_size
            data = bin_f.read(data_length)
            counter = 0

            if e_type == 1:
                while data[counter:counter + e_size]:
                    aer_data = struct.unpack('I', data[counter:counter + 4])[0]
                    timestamp = struct.unpack('I', data[counter + 4:counter + 8])[0] | e_tsoverflow << 31
                    x = (aer_data >> 17) & 0x00007FFF
                    y = (aer_data >> 2) & 0x00007FFF
                    pol = (aer_data >> 1) & 0x00000001
                    counter = counter + e_size
                    txyp['x'].append(x)
                    txyp['y'].append(y)
                    txyp['t'].append(timestamp)
                    txyp['p'].append(pol)
            else:
                # non-polarity event packet, not implemented
                pass
        txyp['x'] = np.asarray(txyp['x'])
        txyp['y'] = np.asarray(txyp['y'])
        txyp['t'] = np.asarray(txyp['t'])
        txyp['p'] = np.asarray(txyp['p'])
        return txyp


def load_ATIS_bin(file_name: str) -> Dict:
    '''
    :param file_name: path of the aedat v3 file
    :type file_name: str
    :return: a dict whose keys are ['t', 'x', 'y', 'p'] and values are ``numpy.ndarray``
    :rtype: Dict

    This function is written by referring to https://github.com/jackd/events-tfds .

    Each ATIS binary example is a separate binary file consisting of a list of events. Each event occupies 40 bits as described below:
    bit 39 - 32: Xaddress (in pixels)
    bit 31 - 24: Yaddress (in pixels)
    bit 23: Polarity (0 for OFF, 1 for ON)
    bit 22 - 0: Timestamp (in microseconds)
    '''
    with open(file_name, 'rb') as bin_f:
        # `& 128` 是取一个8位二进制数的最高位
        # `& 127` 是取其除了最高位，也就是剩下的7位
        raw_data = np.uint32(np.fromfile(bin_f, dtype=np.uint8))
        x = raw_data[0::5]
        y = raw_data[1::5]
        rd_2__5 = raw_data[2::5]
        p = (rd_2__5 & 128) >> 7
        t = ((rd_2__5 & 127) << 16) | (raw_data[3::5] << 8) | (raw_data[4::5])
    return {'t': t, 'x': x, 'y': y, 'p': p}


def load_npz_frames(file_name: str) -> np.ndarray:
    '''
    :param file_name: path of the npz file that saves the frames
    :type file_name: str
    :return: frames
    :rtype: np.ndarray
    '''
    return np.load(file_name)['frames']


class PadFrames:
    def __init__(self, max_frames_number: int):
        '''
        :param max_frames_number: maximum frames number of the padded frames
        :type max_frames_number: int

        This transform will pad the input frame's number to ``max_frames_number``.

        '''
        super().__init__()
        self.max_frames_number = max_frames_number

    def __call__(self, frames: np.ndarray):
        N = frames.shape[0]
        if N < self.max_frames_number:
            padding_frames = np.zeros([self.max_frames_number - N, frames.shape[1], frames.shape[2], frames.shape[3]])
            return np.concatenate((frames, padding_frames), 0)
        else:
            return frames

    def __repr__(self):
        return self.__class__.__name__ + f'(max_frames_number={self.max_frames_number})'


def integrate_events_segment_to_frame(events: Dict, H: int, W: int, j_l: int = 0, j_r: int = -1) -> np.ndarray:
    '''
    :param events: a dict whose keys are ['t', 'x', 'y', 'p'] and values are ``numpy.ndarray``
    :type events: Dict
    :param H: height of the frame
    :type H: int
    :param W: weight of the frame
    :type W: int
    :param j_l: the start index of the integral interval, which is included
    :type j_l: int
    :param j_r: the right index of the integral interval, which is not included
    :type j_r:
    :return: frames
    :rtype: np.ndarray

    Denote a two channels frame as :math:`F` and a pixel at :math:`(p, x, y)` as :math:`F(p, x, y)`, the pixel value is integrated from the events data whose indices are in :math:`[j_{l}, j_{r})`:

.. math::

    F(p, x, y) &= \sum_{i = j_{l}}^{j_{r} - 1} \mathcal{I}_{p, x, y}(p_{i}, x_{i}, y_{i})

where :math:`\lfloor \cdot \rfloor` is the floor operation, :math:`\mathcal{I}_{p, x, y}(p_{i}, x_{i}, y_{i})` is an indicator function and it equals 1 only when :math:`(p, x, y) = (p_{i}, x_{i}, y_{i})`.
    '''
    # 累计脉冲需要用bitcount而不能直接相加，原因可参考下面的示例代码，以及
    # https://stackoverflow.com/questions/15973827/handling-of-duplicate-indices-in-numpy-assignments
    # We must use ``bincount`` rather than simply ``+``. See the following reference:
    # https://stackoverflow.com/questions/15973827/handling-of-duplicate-indices-in-numpy-assignments

    # Here is an example:

    # height = 3
    # width = 3
    # frames = np.zeros(shape=[2, height, width])
    # events = {
    #     'x': np.asarray([1, 2, 1, 1]),
    #     'y': np.asarray([1, 1, 1, 2]),
    #     'p': np.asarray([0, 1, 0, 1])
    # }
    #
    # frames[0, events['y'], events['x']] += (1 - events['p'])
    # frames[1, events['y'], events['x']] += events['p']
    # print('wrong accumulation\n', frames)
    #
    # frames = np.zeros(shape=[2, height, width])
    # for i in range(events['p'].__len__()):
    #     frames[events['p'][i], events['y'][i], events['x'][i]] += 1
    # print('correct accumulation\n', frames)
    #
    # frames = np.zeros(shape=[2, height, width])
    # frames = frames.reshape(2, -1)
    #
    # mask = [events['p'] == 0]
    # mask.append(np.logical_not(mask[0]))
    # for i in range(2):
    #     position = events['y'][mask[i]] * width + events['x'][mask[i]]
    #     events_number_per_pos = np.bincount(position)
    #     idx = np.arange(events_number_per_pos.size)
    #     frames[i][idx] += events_number_per_pos
    # frames = frames.reshape(2, height, width)
    # print('correct accumulation by bincount\n', frames)

    frame = np.zeros(shape=[2, H * W])
    x = events['x'][j_l: j_r].astype(int)  # avoid overflow
    y = events['y'][j_l: j_r].astype(int)
    p = events['p'][j_l: j_r]
    mask = []
    mask.append(p == 0)
    mask.append(np.logical_not(mask[0]))
    for c in range(2):
        position = y[mask[c]] * W + x[mask[c]]
        events_number_per_pos = np.bincount(position)
        frame[c][np.arange(events_number_per_pos.size)] += events_number_per_pos
    return frame.reshape((2, H, W))

def cal_fixed_frames_number_segment_index(events_t: np.ndarray, split_by: str, frames_num: int) -> tuple:
    '''
    :param events_t: events' t
    :type events_t: numpy.ndarray
    :param split_by: 'time' or 'number'
    :type split_by: str
    :param frames_num: the number of frames
    :type frames_num: int
    :return: a tuple ``(j_l, j_r)``
    :rtype: tuple

    Denote ``frames_num`` as :math:`M`, if ``split_by`` is ``'time'``, then

    .. math::

        \\Delta T & = [\\frac{t_{N-1} - t_{0}}{M}] \\\\
        j_{l} & = \\mathop{\\arg\\min}\\limits_{k} \\{t_{k} | t_{k} \\geq t_{0} + \\Delta T \\cdot j\\} \\\\
        j_{r} & = \\begin{cases} \\mathop{\\arg\\max}\\limits_{k} \\{t_{k} | t_{k} < t_{0} + \\Delta T \\cdot (j + 1)\\} + 1, & j <  M - 1 \\cr N, & j = M - 1 \\end{cases}

    If ``split_by`` is ``'number'``, then

    .. math::
        j_{l} & = [\\frac{N}{M}] \\cdot j \\\\
        j_{r} & = \\begin{cases} [\\frac{N}{M}] \\cdot (j + 1), & j <  M - 1 \\cr N, & j = M - 1 \\end{cases}
    '''
    j_l = np.zeros(shape=[frames_num], dtype=int)
    j_r = np.zeros(shape=[frames_num], dtype=int)
    N = events_t.size

    if split_by == 'number':
        di = N // frames_num
        for i in range(frames_num):
            j_l[i] = i * di
            j_r[i] = j_l[i] + di
        j_r[-1] = N

    elif split_by == 'time':
        dt = (events_t[-1] - events_t[0]) // frames_num
        idx = np.arange(N)
        for i in range(frames_num):
            t_l = dt * i + events_t[0]
            t_r = t_l + dt
            mask = np.logical_and(events_t >= t_l, events_t < t_r)
            idx_masked = idx[mask]
            j_l[i] = idx_masked[0]
            j_r[i] = idx_masked[-1] + 1

        j_r[-1] = N
    else:
        raise NotImplementedError

    return j_l, j_r

def integrate_events_by_fixed_frames_number(events: Dict, split_by: str, frames_num: int, H: int, W: int) -> np.ndarray:
    '''
    :param events: a dict whose keys are ['t', 'x', 'y', 'p'] and values are ``numpy.ndarray``
    :type events: Dict
    :param split_by: 'time' or 'number'
    :type split_by: str
    :param frames_num: the number of frames
    :type frames_num: int
    :param H: the height of frame
    :type H: int
    :param W: the weight of frame
    :type W: int
    :return: frames
    :rtype: np.ndarray

    Integrate events to frames by fixed frames number. See ``cal_fixed_frames_number_segment_index`` and ``integrate_events_segment_to_frame`` for more details.
    '''
    j_l, j_r = cal_fixed_frames_number_segment_index(events['t'], split_by, frames_num)
    frames = np.zeros([frames_num, 2, H, W])
    for i in range(frames_num):
        frames[i] = integrate_events_segment_to_frame(events, H, W, j_l[i], j_r[i])
    return frames

def integrate_events_file_to_frames_file_by_fixed_frames_number(events_np_file: str, output_dir: str, split_by: str, frames_num: int, H: int, W: int, print_save: bool = False) -> None:
    '''
    :param events_np_file: path of the events np file
    :type events_np_file: str
    :param output_dir: output directory for saving the frames
    :type output_dir: str
    :param split_by: 'time' or 'number'
    :type split_by: str
    :param frames_num: the number of frames
    :type frames_num: int
    :param H: the height of frame
    :type H: int
    :param W: the weight of frame
    :type W: int
    :param print_save: If ``True``, this function will print saved files' paths.
    :type print_save: bool
    :return: None

    Integrate a events file to frames by fixed frames number and save it. See ``cal_fixed_frames_number_segment_index`` and ``integrate_events_segment_to_frame`` for more details.
    '''
    fname = os.path.join(output_dir, os.path.basename(events_np_file))
    np.savez(fname, frames=integrate_events_by_fixed_frames_number(np.load(events_np_file), split_by, frames_num, H, W))
    if print_save:
        print(f'Frames [{fname}] saved.')



def integrate_events_by_fixed_duration(events: Dict, duration: int, H: int, W: int) -> np.ndarray:
    '''
    :param events: a dict whose keys are ['t', 'x', 'y', 'p'] and values are ``numpy.ndarray``
    :type events: Dict
    :param duration: the time duration of each frame
    :type duration: int
    :param H: the height of frame
    :type H: int
    :param W: the weight of frame
    :type W: int
    :return: frames
    :rtype: np.ndarray

    Integrate events to frames by fixed time duration of each frame.
    '''
    t = events['t']
    N = t.size

    frames = []
    left = 0
    right = 0
    while True:
        t_l = t[left]
        while True:
            if right == N or t[right] - t_l > duration:
                break
            else:
                right += 1
        # integrate from index [left, right)
        frames.append(np.expand_dims(integrate_events_segment_to_frame(events, H, W, left, right), 0))

        left = right

        if right == N:
            return np.concatenate(frames)

def integrate_events_file_to_frames_file_by_fixed_duration(events_np_file: str, output_dir: str, duration: int, H: int, W: int, print_save: bool = False) -> None:
    '''
    :param events_np_file: path of the events np file
    :type events_np_file: str
    :param output_dir: output directory for saving the frames
    :type output_dir: str
    :param duration: the time duration of each frame
    :type duration: int
    :param H: the height of frame
    :type H: int
    :param W: the weight of frame
    :type W: int
    :param print_save: If ``True``, this function will print saved files' paths.
    :type print_save: bool
    :return: None

    Integrate events to frames by fixed time duration of each frame.
    '''
    frames = integrate_events_by_fixed_duration(np.load(events_np_file), duration, H, W)
    fname, _ = os.path.splitext(os.path.basename(events_np_file))
    fname = os.path.join(output_dir, f'{fname}_{frames.shape[0]}.npz')
    np.savez(fname, frames=frames)
    if print_save:
        print(f'Frames [{fname}] saved.')
    return frames.shape[0]


def create_same_directory_structure(source_dir: str, target_dir: str) -> None:
    '''
    :param source_dir: Path of the directory that be copied from
    :type source_dir: str
    :param target_dir: Path of the directory that be copied to
    :type target_dir: str
    :return: None

    Create the same directory structure in ``target_dir`` with that of ``source_dir``.
    '''
    for sub_dir_name in os.listdir(source_dir):
        source_sub_dir = os.path.join(source_dir, sub_dir_name)
        if os.path.isdir(source_sub_dir):
            target_sub_dir = os.path.join(target_dir, sub_dir_name)
            os.mkdir(target_sub_dir)
            print(f'Mkdir [{target_sub_dir}].')
            create_same_directory_structure(source_sub_dir, target_sub_dir)

def split_to_train_test_set(train_ratio: float, origin_dataset: torch.utils.data.Dataset, num_classes: int, random_split: bool = False):
    '''
    :param train_ratio: split the ratio of the origin dataset as the train set
    :type train_ratio: float
    :param origin_dataset: the origin dataset
    :type torch.utils.data.Dataset
    :param num_classes: total classes number, e.g., ``10`` for the MNIST dataset
    :type int
    :param random_split: If ``False``, the front ratio of samples in each classes will
            be included in train set, while the reset will be included in test set.
            If ``True``, this function will split samples in each classes randomly. The randomness is controlled by
            ``numpy.randon.seed``
    :type int
    :return: a tuple ``(train_set, test_set)``
    :rtype: tuple
    '''
    label_idx = []
    for i in range(num_classes):
        label_idx.append([])

    for i, x, y in enumerate(origin_dataset):
        if isinstance(y, np.ndarray) or isinstance(y, torch.Tensor):
            y = y.item()
        label_idx[y].append(i)
    train_idx = []
    test_idx = []
    if random_split:
        for i in range(num_classes):
            np.random.shuffle(label_idx[i])

    for i in range(num_classes):
        pos = math.ceil(label_idx[i].__len__() * train_ratio)
        train_idx.extend(label_idx[i][0: pos])
        test_idx.extend(label_idx[i][pos: label_idx[i].__len__()])

    return torch.utils.data.Subset(origin_dataset, train_idx), torch.utils.data.Subset(origin_dataset, test_idx)





class NeuromorphicDatasetFolder(DatasetFolder):
    def __init__(
            self,
            root: str,
            train: bool = None,
            data_type: str = 'event',
            frames_number: int = None,
            split_by: str = None,
            duration: int = None,
            padding_frame: bool = True,
            transform: Optional[Callable] = None,
            target_transform: Optional[Callable] = None,
    ) -> None:
        '''
        :param root: root path of the dataset
        :type root: str
        :param train: whether use the train set. Set ``True`` or ``False`` for those datasets provide train/test
            division, e.g., DVS128 Gesture dataset. If the dataset does not provide train/test division, e.g., CIFAR10-DVS,
            please set ``None`` and use :name:`~split_to_train_test_set` function to get train/test set
        :type train: bool
        :param data_type: `event` or `frame`
        :type data_type: str
        :param frames_number: the integrated frame number
        :type frames_number: int
        :param split_by: `time` or `number`
        :type split_by: str
        :param duration: the time duration of each frame
        :type duration: int
        :param padding_frame: whether padding the frames number to the maximum number of frames
        :type padding_frame: bool
        :param transform: a function/transform that takes in
            a sample and returns a transformed version.
            E.g, ``transforms.RandomCrop`` for images.
        :type transform: callable
        :param target_transform: a function/transform that takes
            in the target and transforms it.
        :type target_transform: callable

        The base class for neuromorphic dataset. Users can define a new dataset by inheriting this class and implementing
        all abstract methods. Users can refer to ``DVS128Gesture``.

        If ``data_type == 'event'``, the sample in this dataset is a dict whose keys are ['t', 'x', 'y', 'p'] and values
            are ``numpy.ndarray``.

        If ``data_type == 'frame'`` and ``frames_number`` is not ``None``, events will be integrated to frames with fixed
            frames number. ``split_by`` will define how to split events. See ``cal_fixed_frames_number_segment_index`` for
            more details.

        If ``data_type == 'frame'``, ``frames_number`` is ``None``, and ``duration`` is not ``None``, events will be
            integrated to frames with fixed time duration. If ``padding_frame`` is ``True``, each sample will be padded
            to the same frames number (length), which is the maximum frames number of all frames.

        '''

        events_np_root = os.path.join(root, 'events_np')

        if not os.path.exists(events_np_root):

            download_root = os.path.join(root, 'download')

            if os.path.exists(download_root):
                print(f'The [{download_root}] directory for saving downloaed files already exists, check files...')
                # check files
                resource_list = self.resource_url_md5()
                for i in range(resource_list.__len__()):
                    file_name, url, md5 = resource_list[i]
                    fpath = os.path.join(download_root, file_name)
                    if not utils.check_integrity(fpath=fpath, md5=md5):
                        print(f'The file [{fpath}] does not exist or is corrupted.')

                        if os.path.exists(fpath):
                            # If file is corrupted, we will remove it.
                            os.remove(fpath)
                            print(f'Remove [{fpath}]')

                        if self.downloadable():
                            # If file does not exist, we will download it.
                            print(f'Download [{file_name}] from [{url}] to [{download_root}]')
                            utils.download_url(url=url, root=download_root, filename=file_name, md5=md5)
                        else:
                            raise NotImplementedError(
                                f'This dataset can not be downloaded by SpikingJelly, please download [{file_name}] from [{url}] manually and put files at {download_root}.')

            else:
                os.mkdir(download_root)
                print(f'Mkdir [{download_root}] to save downloaded files.')
                resource_list = self.resource_url_md5()
                if self.downloadable():
                    # download and extract file
                    for i in range(resource_list.__len__()):
                        file_name, url, md5 = resource_list[i]
                        print(f'Download [{file_name}] from [{url}] to [{download_root}]')
                        utils.download_url(url=url, root=download_root, filename=file_name, md5=md5)
                else:
                    raise NotImplementedError(f'This dataset can not be downloaded by SpikingJelly, '
                                              f'please download files manually and put files at [{download_root}]. '
                                              f'The resources file_name, url, and md5 are: \n{resource_list}')

            # We have downloaded files and checked files. Now, let us extract the files
            extract_root = os.path.join(root, 'extract')
            if os.path.exists(extract_root):
                print(f'The directory [{extract_root}] for saving extracted files already exists.\n'
                      f'SpikingJelly will not check the data integrity of extracted files.\n'
                      f'If extracted files are not integrated, please delete [{extract_root}] manually, '
                      f'then SpikingJelly will re-extract files from [{download_root}].')
                # shutil.rmtree(extract_root)
                # print(f'Delete [{extract_root}].')
            else:
                os.mkdir(extract_root)
                print(f'Mkdir [{extract_root}].')
                self.extract_downloaded_files(download_root, extract_root)

            # Now let us convert the origin binary files to npz files
            os.mkdir(events_np_root)
            print(f'Mkdir [{events_np_root}].')
            print(f'Start to convert the origin data from [{extract_root}] to [{events_np_root}] in np.ndarray format.')
            self.create_events_np_files(extract_root, events_np_root)

        H, W = self.get_H_W()

        if data_type == 'event':
            _root = events_np_root
            _loader = np.load
            _transform = transform
            _target_transform = target_transform

        elif data_type == 'frame':
            if frames_number is not None:
                assert frames_number > 0 and isinstance(frames_number, int)
                assert split_by == 'time' or split_by == 'number'
                frames_np_root = os.path.join(root, f'frames_number_{frames_number}_split_by_{split_by}')
                if os.path.exists(frames_np_root):
                    print(f'The directory [{frames_np_root}] already exists.')
                else:
                    os.mkdir(frames_np_root)
                    print(f'Mkdir [{frames_np_root}].')

                    # create the same directory structure
                    create_same_directory_structure(events_np_root, frames_np_root)

                    # use multi-thread to accelerate
                    t_ckp = time.time()
                    with ThreadPoolExecutor(max_workers=min(multiprocessing.cpu_count(), 64)) as tpe:
                        print(f'Start ThreadPoolExecutor with max workers = [{tpe._max_workers}].')
                        for e_root, e_dirs, e_files in os.walk(events_np_root):
                            if e_files.__len__() > 0:
                                output_dir = os.path.join(frames_np_root, os.path.relpath(e_root, events_np_root))
                                for e_file in e_files:
                                    events_np_file = os.path.join(e_root, e_file)
                                    print(f'Start to integrate [{events_np_file}] to frames and save to [{output_dir}].')
                                    tpe.submit(integrate_events_file_to_frames_file_by_fixed_frames_number, events_np_file, output_dir, split_by, frames_number, H, W, True)

                    print(f'Used time = [{round(time.time() - t_ckp, 2)}s].')

                _root = frames_np_root
                _loader = load_npz_frames
                _transform = transform
                _target_transform = target_transform

            elif duration is not None:
                self.max_frames_number = 0
                assert duration > 0 and isinstance(duration, int)
                frames_np_root = os.path.join(root, f'duration_{duration}')
                if os.path.exists(frames_np_root):
                    print(f'The directory [{frames_np_root}] already exists.')
                    fn_name = os.path.join(frames_np_root, 'max_frames_number.npy')
                    self.max_frames_number = np.load(fn_name).item()
                    print(f'max_frames_number = [{self.max_frames_number}].')

                else:
                    os.mkdir(frames_np_root)
                    print(f'Mkdir [{frames_np_root}].')
                    # create the same directory structure
                    create_same_directory_structure(events_np_root, frames_np_root)
                    # use multi-thread to accelerate
                    t_ckp = time.time()
                    future_list = []
                    with ThreadPoolExecutor(max_workers=min(multiprocessing.cpu_count(), 64)) as tpe:
                        print(f'Start ThreadPoolExecutor with max workers = [{tpe._max_workers}].')
                        for e_root, e_dirs, e_files in os.walk(events_np_root):
                            if e_files.__len__() > 0:
                                output_dir = os.path.join(frames_np_root, os.path.relpath(e_root, events_np_root))
                                for e_file in e_files:
                                    events_np_file = os.path.join(e_root, e_file)
                                    print(f'Start to integrate [{events_np_file}] to frames and save to [{output_dir}].')
                                    future_list.append(tpe.submit(integrate_events_file_to_frames_file_by_fixed_duration, events_np_file, output_dir, duration, H, W, True))

                    for future in future_list:
                        self.max_frames_number = max(self.max_frames_number, future.result())

                    # save the max_frames_number in frames_np_root
                    fn_name = os.path.join(frames_np_root, 'max_frames_number.npy')
                    np.save(fn_name, self.max_frames_number)
                    print(f'Save max_frames_number to [{fn_name}].')
                    print(f'Used time = [{round(time.time() - t_ckp, 2)}s].')

                if padding_frame:
                    if transform is None:
                        transform_with_padding = PadFrames(self.max_frames_number)
                    else:
                        transform_with_padding = transforms.Compose([PadFrames(self.max_frames_number), transform])

                    _root = frames_np_root
                    _loader = load_npz_frames
                    _transform = transform_with_padding
                    _target_transform = target_transform

                else:
                    _root = frames_np_root
                    _loader = load_npz_frames
                    _transform = transform
                    _target_transform = target_transform

            else:
                raise ValueError('frames_number and duration can not both be None.')

        if train is not None:
            if train:
                _root = os.path.join(_root, 'train')
            else:
                _root = os.path.join(_root, 'test')

        super().__init__(root=_root, loader=_loader, extensions='.npz', transform=_transform,
                         target_transform=_target_transform)

    @staticmethod
    @abstractmethod
    def load_origin_data(file_name: str) -> Dict:
        '''
        :param file_name: path of the events file
        :type file_name: str
        :return: a dict whose keys are ['t', 'x', 'y', 'p'] and values are ``numpy.ndarray``
        :rtype: Dict

        This function defines how to read the origin binary data.
        '''
        pass

    @staticmethod
    @abstractmethod
    def resource_url_md5() -> list:
        '''
        :return: A list ``url`` that ``url[i]`` is a tuple, which contains the i-th file's name, download link, and MD5
        :rtype: list
        '''
        pass

    @staticmethod
    @abstractmethod
    def downloadable() -> bool:
        '''
        :return: Whether the dataset can be directly downloaded by python codes. If not, the user have to download it manually
        :rtype: bool
        '''
        pass

    @staticmethod
    @abstractmethod
    def extract_downloaded_files(download_root: str, extract_root: str):
        '''
        :param download_root: Root directory path which saves downloaded dataset files
        :type download_root: str
        :param extract_root: Root directory path which saves extracted files from downloaded files
        :type extract_root: str
        :return: None

        This function defines how to extract download files.
        '''
        pass

    @staticmethod
    @abstractmethod
    def create_events_np_files(extract_root: str, events_np_root: str):
        '''
        :param extract_root: Root directory path which saves extracted files from downloaded files
        :type extract_root: str
        :param events_np_root: Root directory path which saves events files in the ``npz`` format
        :type events_np_root:
        :return: None

        This function defines how to convert the origin binary data in ``extract_root`` to ``npz`` format and save converted files in ``events_np_root``.
        '''
        pass

    @staticmethod
    @abstractmethod
    def get_H_W() -> Tuple:
        '''
        :return: A tuple ``(H, W)``, where ``H`` is the height of the data and ``W` is the weight of the data.
            For example, this function returns ``(128, 128)`` for the DVS128 Gesture dataset.
        :rtype: tuple
        '''
        pass



