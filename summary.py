#!/usr/bin/env python

import argparse
import io
from collections.abc import Iterator
from pathlib import Path
from zipfile import ZipFile

import matplotlib.pyplot as plt
import numpy as np
import pydicom
import skimage as ski
from rectpack import newPacker
from tqdm import tqdm

X = 1080 * 2
Y = 1920 * 2


def compress_image(image):
    i = image == np.roll(image,1,axis=0)
    j = image == np.roll(image,1,axis=1)
    k = ski.morphology.binary_erosion(i & j, np.ones((5,5,), dtype=bool))
    image = np.delete(image, np.all(k, axis=1), axis=0)
    image = np.delete(image, np.all(k, axis=0), axis=1)
    return image


def preprocess_image(dcm: pydicom.FileDataset) -> np.ndarray:
    img = ski.util.img_as_float32(dcm.pixel_array)
    match dcm.PhotometricInterpretation:
        case "MONOCHROME1":
            img = ski.util.invert(img)
        case "MONOCHROME2":
            pass
    bot, top = img.min(), img.max()
    d = 0.01 * (top - bot) / 100
    bot, top = bot + d, top - d
    bot, top = np.quantile(img[(img > bot) & (img < top)], [0.01, 0.99])
    d = 0.02 * (top - bot)
    bot, top = bot + d, top - d
    img = ski.exposure.rescale_intensity(img, (bot, top))
    img = np.clip(img, 0, 1)
    img = ski.exposure.equalize_adapthist(img, 96)
    return compress_image(img)


def pack_images(images: list[np.ndarray]) -> list[np.ndarray]:
    packer = newPacker()
    packer.add_bin(X, Y, float("inf"))
    for i, image in enumerate(images):
        packer.add_rect(*image.shape, i)
    packer.pack()
    canvas = [np.zeros((X, Y), dtype=np.float32) for _ in range(len(packer))]
    unused = set(range(len(images)))
    for i, x, y, w, h, j in packer.rect_list():
        if (w, h) == images[j].shape:
            canvas[i][x : x + w, y : y + h] = images[j]
        else:
            canvas[i][x : x + w, y : y + h] = np.rot90(images[j])
        unused.remove(j)
    return canvas, unused


def images_from_zip(path: Path) -> Iterator[np.ndarray]:
    with ZipFile(path) as zf:
        files = [
            item.filename for item in zf.infolist() if item.filename.endswith(".dcm")
        ]
        for file in tqdm(files, desc=path.name, position=1, leave=False):
            with zf.open(file, "r") as fp:
                dcm = pydicom.dcmread(io.BytesIO(fp.read()))
                yield preprocess_image(dcm)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("sources", nargs="*")
    args = parser.parse_args()
    cm = plt.get_cmap("inferno")
    sources = [Path(s) for s in args.sources]
    for source in tqdm(sources, position=0):
        images = list(images_from_zip(source))
        packed, unused = pack_images(images)
        for i, canvas in enumerate(packed, start=1):
            ski.io.imsave(
                source.with_suffix(f".{i:03d}.jpg").name,
                ski.util.img_as_ubyte(cm(canvas)[:, :, :3]),
            )
        unused = [images[i] for i in unused]
        for i, image in enumerate(unused, start=100):
            ski.io.imsave(
                source.with_suffix(f".{i:03d}.jpg").name,
                ski.util.img_as_ubyte(cm(image)[:, :, :3]),
            )
