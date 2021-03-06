import sys
from pathlib import Path
import subprocess as sp
import random

import numpy as np

from tqdm import tqdm
import torch

from .utils.audio import AudioDataset, AudioDataLoader, Int2OneHot
from .utils.kaldi_io import smart_open, read_string, read_vec_int
from .utils.logger import logger
from .utils import params as p


"""
This recipe requires Kaldi's egs/aspire/s5 recipe directory containing the result
of its own scripts, especially the data/ and the exp/
"""

KALDI_ROOT = Path("/home/jbaik/kaldi").resolve()
ASPIRE_ROOT = Path(KALDI_ROOT, "egs/aspire/ics").resolve()
DATA_ROOT = (Path(__file__).parent / "data" / "aspire").resolve()

assert KALDI_ROOT.exists(), \
    f"no such path \"{str(KALDI_ROOT)}\" not found"
assert ASPIRE_ROOT.exists(), \
    f"no such path \"{str(ASPIRE_ROOT)}\" not found"

WIN_SAMP_SIZE = p.SAMPLE_RATE * p.WINDOW_SIZE
WIN_SAMP_SHIFT = p.SAMPLE_RATE * p.WINDOW_SHIFT
SAMPLE_MARGIN = WIN_SAMP_SHIFT * p.FRAME_MARGIN  # samples


def get_num_lines(filename):
    import mmap

    with open(filename, "r+") as f:
        buf = mmap.mmap(f.fileno(), 0)
        lines = 0
        while buf.readline():
            lines += 1
    return lines


def strip_text(text):
    mask = "abcdefghijklmnopqrstuvwxyz'- "
    stripped = [x for x in text.lower() if x in mask]
    return ''.join(stripped)


def split_wav(mode, target_dir):
    import io
    import wave

    data_dir = Path(ASPIRE_ROOT, "data", mode).resolve()
    segments_file = Path(data_dir, "segments")
    logger.info(f"processing {str(segments_file)} file ...")
    segments = dict()
    with smart_open(segments_file, "r") as f:
        for line in tqdm(f, total=get_num_lines(segments_file)):
            split = line.strip().split()
            uttid, wavid, start, end = split[0], split[1], float(split[2]), float(split[3])
            if wavid in segments:
                segments[wavid].append((uttid, start, end))
            else:
                segments[wavid] = [(uttid, start, end)]

    wav_scp = Path(data_dir, "wav.scp")
    logger.info(f"processing {str(wav_scp)} file ...")
    manifest = dict()
    with smart_open(wav_scp, "r") as f:
        for line in tqdm(f, total=get_num_lines(wav_scp)):
            wavid, cmd = line.strip().split(" ", 1)
            if not wavid in segments:
                continue
            cmd = cmd.strip().rstrip(' |').split()
            p = sp.run(cmd, stdout=sp.PIPE, stderr=sp.PIPE)
            fp = io.BytesIO(p.stdout)
            with wave.openfp(fp, "rb") as wav:
                fr = wav.getframerate()
                nf = wav.getnframes()
                for uttid, start, end in segments[wavid]:
                    fs, fe = int(fr * start - SAMPLE_MARGIN), int(fr * end + SAMPLE_MARGIN)
                    if fs < 0 or fe > nf:
                        continue
                    wav.rewind()
                    wav.setpos(fs)
                    signal = wav.readframes(fe - fs)
                    tar_dir = Path(target_dir) / uttid[6:9]
                    Path(tar_dir).mkdir(mode=0o755, parents=True, exist_ok=True)
                    wav_file = str(Path(tar_dir, uttid + ".wav"))
                    with wave.open(wav_file, "wb") as split_wav:
                        split_wav.setparams(wav.getparams())
                        split_wav.writeframes(signal)
                    manifest[uttid] = (wav_file, fe - fs)
    return manifest


def get_transcripts(mode, target_dir):
    data_dir = Path(ASPIRE_ROOT, "data", mode).resolve()
    texts_file = Path(data_dir, "text")
    logger.info(f"processing {str(texts_file)} file ...")
    manifest = dict()
    with smart_open(Path(data_dir, "text"), "r") as f:
        for line in tqdm(f, total=get_num_lines(texts_file)):
            uttid, text = line.strip().split(" ", 1)
            text = strip_text(text)
            tar_dir = Path(target_dir) / uttid[6:9]
            Path(tar_dir).mkdir(mode=0o755, parents=True, exist_ok=True)
            txt_file = str(Path(tar_dir, uttid + ".txt"))
            with open(txt_file, "w") as txt:
                txt.write(text + "\n")
            manifest[uttid] = (txt_file, text)
    return manifest


def get_alignments(target_dir):
    import io
    import pipes
    import gzip

    exp_dir = Path(ASPIRE_ROOT, "exp", "tri5a").resolve()
    models = exp_dir.glob("*.mdl")
    model = sorted(models, key=lambda x: x.stat().st_mtime)[-1]

    logger.info("processing alignment files ...")
    manifest = dict()
    alis = [x for x in exp_dir.glob("ali.*.gz")]
    for ali in tqdm(alis):
        cmd = [ str(Path(KALDI_ROOT, "src", "bin", "ali-to-phones")),
                "--per-frame", f"{model}", f"ark:-", f"ark,f:-" ]
        with gzip.GzipFile(ali, "rb") as a:
            p = sp.run(cmd, stdout=sp.PIPE, stderr=sp.PIPE, input=a.read())
            with io.BytesIO(p.stdout) as f:
                while True:
                    try:
                        uttid = read_string(f)
                    except ValueError:
                        break
                    phones = read_vec_int(f)
                    num_frms = len(phones)
                    tar_dir = Path(target_dir) / uttid[6:9]
                    Path(tar_dir).mkdir(mode=0o755, parents=True, exist_ok=True)
                    phn_file = str(Path(tar_dir, uttid + ".phn"))
                    np.savetxt(phn_file, phones, "%d")
                    manifest[uttid] = (phn_file, num_frms, phones)
    return manifest


def prepare_data(target_dir):
    """
    since the target time-alignment exists only on the train set,
    we split the train set into train and dev set
    """
    #train_wav_manifest = split_wav("train", DATA_ROOT)
    train_txt_manifest = get_transcripts("train", DATA_ROOT)
    #phn_manifest = get_alignments(DATA_ROOT)

    logger.info("generating manifest files ...")
    with open(Path(target_dir, "train.csv"), "w") as f1:
        with open(Path(target_dir, "dev.csv"), "w") as f2:
            for k, v in train_wav_manifest.items():
                if not k in train_txt_manifest:
                    continue
                if not k in phn_manifest:
                    continue
                wav_file, samples = v
                txt_file, _ = train_txt_manifest[k]
                phn_file, num_frms, _ = phn_manifest[k]
                if 0 < int(k[6:11]) < 60:
                    f2.write(f"{k},{wav_file},{samples},{phn_file},{num_frms},{txt_file}\n")
                else:
                    f1.write(f"{k},{wav_file},{samples},{phn_file},{num_frms},{txt_file}\n")
    logger.info("data preparation finished.")


def reconstruct_manifest(target_dir):
    import wave

    logger.info("reconstructing manifest files ...")
    with open(Path(target_dir, "train.csv"), "w") as f1:
        with open(Path(target_dir, "dev.csv"), "w") as f2:
            for wav_file in Path(target_dir).glob("**/*.wav"):
                uttid = wav_file.stem
                txt_file = str(wav_file).replace("wav", "txt")
                phn_file = str(wav_file).replace("wav", "phn")
                if not Path(txt_file).exists() or not Path(phn_file).exists():
                    continue
                with wave.openfp(str(wav_file), "rb") as wav:
                    samples = wav.getnframes()
                num_frms = get_num_lines(phn_file)
                if 0 < int(uttid[6:11]) < 60:
                    f2.write(f"{uttid},{wav_file},{samples},{phn_file},{num_frms},{txt_file}\n")
                else:
                    f1.write(f"{uttid},{wav_file},{samples},{phn_file},{num_frms},{txt_file}\n")
    logger.info("data preparation finished.")


def _samples2frames(samples):
    num_samples = samples - 2 * SAMPLE_MARGIN
    return int((num_samples - WIN_SAMP_SIZE) // WIN_SAMP_SHIFT + 1)


class Aspire(AudioDataset):
    """Kaldi's ASpIRE recipe (LDC Fisher dataset)
       loading Kaldi's frame-aligned phones target and the corresponding audio files

    Args:
        mode (str): either one of "train", "dev", or "test"
        data_dir (path): dir containing the processed data and manifests
    """
    root = DATA_ROOT
    entries = list()
    entry_frames = list()

    def __init__(self, root=None, mode=None, data_size=1e30, *args, **kwargs):
        assert mode in ["train_sup", "train_unsup", "train", "dev", "test"], \
            "invalid mode options: either one of \"train_sup\", \"train_unsup\", \"train\", \"dev\", or \"test\""
        self.mode = mode
        self.data_size = data_size
        if root is not None:
            self.root = Path(root).resolve()
        self._load_manifest()
        super().__init__(frame_margin=p.FRAME_MARGIN, unit_frames=p.HEIGHT,
                         window_shift=p.WINDOW_SHIFT, window_size=p.WINDOW_SIZE,
                         target_transform=Int2OneHot(p.NUM_LABELS), *args, **kwargs)

    def __getitem__(self, index):
        uttid, wav_file, samples, phn_file, num_phns, txt_file = self.entries[index]
        # read and transform wav file
        if self.transform is not None:
            tensors = self.transform(wav_file)
        if self.mode == "train_unsup":
            return tensors, None
        # read phn file
        targets = np.loadtxt(phn_file, dtype="int").tolist()
        if self.target_transform is not None:
            targets = self.target_transform(targets)
        # manipulating when the length of data and targets are mismatched
        l0, l1 = len(tensors), len(targets)
        if l0 > l1:
            tensors = tensors[:l1]
        elif l0 < l1:
            tensors.extend([torch.zeros_like(tensors[0]) for i in range(l1 - l0)])
        return tensors, targets

    def __len__(self):
        return len(self.entries)

    def _load_manifest(self):
        manifest_file = self.root / f"{self.mode}.csv"
        if not self.root.exists() or not manifest_file.exists():
            logger.error(f"no such path {self.root} or manifest file {manifest_file} found. "
                         f"need to run 'python {__file__}' first")
            sys.exit(1)

        logger.info(f"loading dataset manifest {manifest_file} ...")
        with open(manifest_file, "r") as f:
            manifest = f.readlines()
        self.entries = [tuple(x.strip().split(',')) for x in manifest]
        # drop short entries less than 1 sec
        self.entries = [e for e in self.entries if (_samples2frames(int(e[2])) > 100)]
        # randomly choose a number of data_size
        size = min(self.data_size, len(manifest))
        self.entries = random.sample(self.entries, size)
        if self.mode == "train_unsup":
            self.entry_frames = [_samples2frames(int(e[2])) for e in self.entries]
        else:
            self.entry_frames = [int(e[4]) for e in self.entries]
        logger.info(f"{len(self.entries)} entries, {sum(self.entry_frames)} frames are loaded.")


if __name__ == "__main__":
    if False:
        reconstruct_manifest(DATA_ROOT)

    if True:
        train_dataset = Aspire(mode="test")
        loader = AudioDataLoader(train_dataset, batch_size=10, num_workers=4, shuffle=True)
        print(f"num_workers={loader.num_workers}")

        for i, data in enumerate(loader):
            tensors, targets = data
            #for tensors, targets in data:
            print(tensors, targets)
            if False:
                import matplotlib
                matplotlib.use('TkAgg')
                matplotlib.interactive(True)
                import matplotlib.pyplot as plt

                for tensor, target in zip(tensors, targets):
                    tensor = tensor.view(-1, p.CHANNEL, p.WIDTH, p.HEIGHT)
                    t = np.arange(0, tensor.size(3)) / 8000
                    f = np.linspace(0, 4000, tensor.size(2))

                    fig = plt.figure(1)
                    p = plt.pcolormesh(t, f, np.log10(10 ** tensor[0][0] - 1), cmap='plasma')
                    plt.colorbar(p)
                    plt.show(block=True)
            if i == 2:
                break
        #plt.close('all')
