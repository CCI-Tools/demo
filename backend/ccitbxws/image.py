import io
import sys
import uuid
from abc import ABCMeta, abstractmethod, abstractproperty

import matplotlib.cm as cm
import numpy as np
from PIL import Image

from .cache import Cache, MemoryCacheStore
from .utils import *

_DEFAULT_TILE_CACHE = None


class MemoryTileCacheStore(MemoryCacheStore):
    def store_value(self, key, tile):
        if hasattr(tile, 'nbytes'):
            # A numpy ndarray instance
            size = tile.nbytes
        elif hasattr(tile, 'size') and hasattr(tile, 'mode'):
            # A PIL Image instance
            w, h = tile.size
            m = tile.mode
            size = w * h * (4 if m in ('RGBA', 'RGBx', 'I', 'F') else
                            3 if m in ('RGB', 'YCbCr', 'LAB', 'HSV') else
                            1. / 8. if m == '1' else
                            1)
        else:
            size = sys.getsizeof(tile)
        return tile, size


def set_default_tile_cache(cache=None, no_cache=False, capacity=64 * 1024 * 1024, threshold=0.75):
    global _DEFAULT_TILE_CACHE
    if no_cache:
        _DEFAULT_TILE_CACHE = None
    elif cache is None:
        _DEFAULT_TILE_CACHE = Cache(MemoryTileCacheStore(), capacity=capacity, threshold=threshold)
    else:
        _DEFAULT_TILE_CACHE = cache


def get_default_tile_cache():
    global _DEFAULT_TILE_CACHE
    return _DEFAULT_TILE_CACHE


class TiledImage(metaclass=ABCMeta):
    """
    The interface for tiled images.
    """

    @abstractproperty
    def id(self):
        """
        Return a unique image identifier.
        :return: A unique (string) object
        """
        pass

    @abstractproperty
    def format(self):
        """
        Return a format string such as 'PNG', 'JPG', 'RAW', etc, or None according to PIL.
        :return: A string indicating the image (file) format.
        """
        pass

    @abstractproperty
    def mode(self):
        """
        Return the image mode string such as 'RGBA', 'RGB', 'L', etc, or None according to PIL.
        See http://pillow.readthedocs.org/en/3.0.x/handbook/concepts.html#modes
        :return: A string indicating the image mode
        """
        pass

    @abstractproperty
    def size(self):
        """
        :return: The size of the image as a (width, height) tuple
        """
        pass

    @abstractproperty
    def tile_size(self):
        """
        :return: The size of the image as a (tile_width, tile_height) tuple
        """
        pass

    @abstractproperty
    def num_tiles(self):
        """
        :return: The number of tiles as a (num_tiles_x, num_tiles_y) tuple
        """
        pass

    @abstractmethod
    def get_tile(self, tile_x, tile_y):
        """
        :param tile_y: the tile coordinate in X direction
        :param tile_x: the tile coordinate in Y direction
        :return: The image's tile data at tile_x, tile_y.
        """
        pass

    @abstractmethod
    def dispose(self):
        """
        Disposes this images.
        """
        pass


class AbstractTiledImage(TiledImage, metaclass=ABCMeta):
    """
    An abstract base class for tiled images.
    Derived classes must implement the get_tile(tile_x, tile_y) method.
    It is strongly advised to also override the and the dispose() method in order to release any allocated resources.
    """

    def __init__(self, size, tile_size=None, num_tiles=None, mode=None, format=None, image_id=None):
        self._width = size[0]
        self._height = size[1]
        self._tile_width = tile_size[0] if tile_size else compute_tile_size(self._width)
        self._tile_height = tile_size[1] if tile_size else compute_tile_size(self._height)
        self._num_tiles_x = num_tiles[0] if num_tiles else cardinal_div_round(self._width, self._tile_width)
        self._num_tiles_y = num_tiles[1] if num_tiles else cardinal_div_round(self._height, self._tile_height)
        self._id = image_id if image_id else str(uuid.uuid4)
        self._mode = mode
        self._format = format

    @property
    def id(self):
        return self._id

    @property
    def format(self):
        return self._format

    @property
    def mode(self):
        return self._mode

    @property
    def size(self):
        return self._width, self._height

    @property
    def tile_size(self):
        return self._tile_width, self._tile_height

    @property
    def num_tiles(self):
        return self._num_tiles_x, self._num_tiles_y

    def dispose(self):
        """
        Does nothing.
        """
        pass

    def get_tile_id(self, tile_x, tile_y):
        return '%s/%d/%d' % (self.id, tile_y, tile_x)


class OpImage(AbstractTiledImage, metaclass=ABCMeta):
    """
    An abstract base class for images that compute their tiles.
    Derived classes must implement the compute_tile(tile_x, tile_y, rect) method only.
    """

    def __init__(self, size, tile_size=None, num_tiles=None, mode=None, format=None, image_id=None, tile_cache=None):
        """
        Constructor.
        """
        super().__init__(size, tile_size=tile_size, num_tiles=num_tiles, mode=mode, format=format, image_id=image_id)
        self._tile_cache = tile_cache if tile_cache is not None else get_default_tile_cache()

    @property
    def tile_cache(self):
        return self._tile_cache

    def get_tile(self, tile_x, tile_y):
        tile_id = None
        cache = self._tile_cache
        if cache:
            tile_id = self.get_tile_id(tile_x, tile_y)
            tile = cache.get_value(tile_id)
            if tile is not None:
                return tile
        tw, th = self.tile_size
        tile = self.compute_tile(tile_x, tile_y, (tw * tile_x, th * tile_y, tw, th))
        if cache:
            cache.put_value(tile_id, tile)
        return tile

    @abstractmethod
    def compute_tile(self, tile_x, tile_y, rectangle):
        pass

    def dispose(self):
        cache = self._tile_cache
        if cache:
            num_tiles_x, num_tiles_y = self.num_tiles
            for tile_y in range(num_tiles_y):
                for tile_x in range(num_tiles_x):
                    cache.remove_value(self.get_tile_id(tile_x, tile_y))


class DecoratorImage(OpImage, metaclass=ABCMeta):
    """
    Abstract tiled image class allowing behavior to be added to a given tiled source image.
    The decorator image will have the same image layout as the source image.
    Derived classes must implement the compute_tile_from_source_tile() method only.
    """

    def __init__(self,
                 source_image,
                 size=None,
                 tile_size=None,
                 num_tiles=None,
                 image_id=None,
                 format=None,
                 mode=None,
                 tile_cache=None):
        """
        Constructor.
        :param source_image: a tiled source image, see TiledImage type
        :param image_id: an optional ID primarily used for caching
        :param tile_cache: an optional tile cache, see Cache type
        """
        super().__init__(size if size else source_image.size,
                         tile_size=tile_size if tile_size else source_image.tile_size,
                         num_tiles=num_tiles if num_tiles else source_image.num_tiles,
                         mode=mode if mode else source_image.mode,
                         format=format if format else source_image.format,
                         image_id=image_id,
                         tile_cache=tile_cache)
        self._source_image = source_image

    @property
    def source_image(self):
        return self._source_image

    def compute_tile(self, tile_x, tile_y, rectangle):
        source_tile = self._source_image.get_tile(tile_x, tile_y)
        target_tile = None
        if source_tile is not None:
            target_tile = self.compute_tile_from_source_tile(tile_x, tile_y, rectangle, source_tile)
        return target_tile

    @abstractmethod
    def compute_tile_from_source_tile(self, tile_x, tile_y, rectangle, source_tile):
        pass


class TransformArrayImage(DecoratorImage):
    """
    Performs basic (numpy) array transforms. Currently available: force_masked, flip_y.
    Expects the source image to provide (numpy) arrays.
    """
    def __init__(self, source_image, flip_y=False, force_masked=True, no_data_value=None, tile_cache=None):
        super().__init__(source_image, tile_cache=tile_cache)
        self._force_masked = force_masked
        self._flip_y = flip_y
        self._no_data_value = no_data_value

    @property
    def no_data_value(self):
        return self._no_data_value

    def compute_tile(self, tile_x, tile_y, rectangle):
        if self._flip_y:
            num_tiles_y = self.num_tiles[1]
            tile_size_y = self.tile_size[1]
            tile_y = num_tiles_y - 1 - tile_y
            x, y, w, h = rectangle
            rectangle = x, tile_y * tile_size_y, w, h
        source_tile = self._source_image.get_tile(tile_x, tile_y)
        target_tile = None
        if source_tile is not None:
            target_tile = self.compute_tile_from_source_tile(tile_x, tile_y, rectangle, source_tile)
        return target_tile

    def compute_tile_from_source_tile(self, tile_x, tile_y, rectangle, tile):
        if self._flip_y:
            # Flip tile using fancy indexing
            tile = tile[..., ::-1, :]
        if self._force_masked and not np.ma.is_masked(tile):
            # if tile is not masked
            if self._no_data_value is not None:
                # and we have a fill value, return a masked tile
                tile = np.ma.masked_equal(tile, self._no_data_value)
            elif np.issubdtype(tile.dtype, float) or np.issubdtype(tile.dtype, complex):
                # and it is of float type, return a masked tile with a mask from invalids, i.e. NaN, -Inf, +Inf
                tile = np.ma.masked_invalid(tile)
        return tile


class ColorMappedRgbaImage(DecoratorImage):
    """
    Creates a color-mapped image from a source image that provide tiles as numpy-like image arrays.
    """

    def __init__(self, source_image, value_range=(0.0, 1.0), cmap_name=None, num_colors=256,
                 no_data_value=None, encode=False, format=None, tile_cache=None):
        """
        Constructor.

        :param source_image:
        :param value_range:
        :param cmap_name:
        :param num_colors:
        :param no_data_value: fill value which will be mapped
        :param encode:
        :param format:
        :return:
        """
        super().__init__(source_image, format=format, mode='RGBA', tile_cache=tile_cache)
        self._value_range = value_range
        self._cmap_name = cmap_name if cmap_name else 'jet'
        self._cmap = cm.get_cmap(self._cmap_name, num_colors)
        self._cmap.set_bad('k', 0)
        self._no_data_value = no_data_value
        self._encode = encode

    def compute_tile_from_source_tile(self, tile_x, tile_y, rectangle, source_tile):
        value_min, value_max = self._value_range
        if not np.ma.is_masked(source_tile):
            if self._no_data_value is not None:
                array = np.ma.masked_equal(source_tile, self._no_data_value)
                array = array.clip(value_min, value_max, out=array)
            elif np.issubdtype(source_tile.dtype, float) or np.issubdtype(source_tile.dtype, complex):
                array = np.ma.masked_invalid(source_tile)
                array = array.clip(value_min, value_max, out=array)
            else:
                array = source_tile.clip(value_min, value_max)
        else:
            array = source_tile.clip(value_min, value_max)

        old_shape = array.shape
        array = np.reshape(array, (old_shape[-2], old_shape[-1]))
        # check if we can optimize the following calls by using Numexpr
        # see https://github.com/pydata/numexpr/wiki/Numexpr-Users-Guide
        array -= value_min
        array *= 1.0 / (value_max - value_min)
        array = self._cmap(array, bytes=True)
        image = Image.fromarray(array, mode=self.mode)

        if self._encode and self.format:
            ostream = io.BytesIO()
            image.save(ostream, format=self.format)
            encoded_image = ostream.getvalue()
            ostream.close()
            return encoded_image
        else:
            return image

    def create_pyramid(self):
        if self._encode:
            raise TypeError("can't create pyramid from encoded hi-res tiles")
        return ImagePyramid.create_from_image(self, create_pil_downsampling_image)


class DownsamplingImage(OpImage):
    """
    Abstract base class for images that downsample a tiled source image.
    Derived classes must implement the aggregate_and_stitch_source_tiles() method only.
    """

    def __init__(self,
                 source_image,
                 image_id=None,
                 tile_cache=None):
        """
        Constructor.
        :param source_image: a tiled source image (type TiledImage) whose source tiles must be PIL Images
        :param image_id: an optional key prefix string used for caching
        :param tile_cache: an optional tile cache of type Cache
        """
        w, h = source_image.size
        nx, ny = source_image.num_tiles
        super().__init__((w // 2, h // 2),
                         tile_size=source_image.tile_size,
                         num_tiles=(nx // 2, ny // 2),
                         image_id=image_id,
                         mode=source_image.mode,
                         format=source_image.format,
                         tile_cache=tile_cache)
        self._source_image = source_image

    @property
    def source_image(self):
        return self._source_image

    def compute_tile(self, tile_x, tile_y, rectangle):
        source_tile_x = 2 * tile_x
        source_tile_y = 2 * tile_y

        source_image = self._source_image
        source_tiles = (source_image.get_tile(source_tile_x, source_tile_y),
                        source_image.get_tile(source_tile_x, source_tile_y + 1),
                        source_image.get_tile(source_tile_x + 1, source_tile_y),
                        source_image.get_tile(source_tile_x + 1, source_tile_y + 1))

        target_width, target_height = self.tile_size
        target_width_h, target_height_h = target_width // 2, target_height // 2

        target_positions = ((0, 0),
                            (0, target_height_h),
                            (target_width_h, 0),
                            (target_width_h, target_height_h))

        return self.aggregate_and_stitch_source_tiles(source_tiles,
                                                      (target_width, target_height),
                                                      target_positions)

    @abstractmethod
    def aggregate_and_stitch_source_tiles(self, source_tiles, target_size, target_positions):
        pass


class PilDownsamplingImage(DownsamplingImage):
    """
    Downsamples a tiled source image whose tiles are PIL Images.
    See http://pillow.readthedocs.org
    """

    def __init__(self,
                 source_image,
                 image_id=None,
                 tile_cache=None,
                 resampling=Image.ANTIALIAS):
        """
        Constructor.
        :param source_image: a tiled source image (type TiledImage) whose source tiles must be PIL Images
        :param image_id: an optional ID primarily used for caching
        :param tile_cache: an optional tile cache of type Cache
        :param resampling: the PIL Image resampling filter, default is PIL.Image.ANTIALIAS.
               See http://pillow.readthedocs.org/en/3.0.x/handbook/concepts.html#filters
               See http://pillow.readthedocs.org/en/3.0.x/reference/Image.html#PIL.Image.Image.resize
        """
        super().__init__(source_image, image_id=image_id, tile_cache=tile_cache)
        self._resampling = resampling

    @property
    def resampling(self):
        return self._resampling

    def aggregate_and_stitch_source_tiles(self, source_tiles, target_size, target_positions):
        target_shape = (target_size[0] // 2, target_size[1] // 2)
        agg_tiles = [source_tile.resize(target_shape, self._resampling) for source_tile in source_tiles]
        target_tile = Image.new(self._source_image.mode, target_size)
        for i in range(len(agg_tiles)):
            target_tile.paste(agg_tiles[i], target_positions[i])
        return target_tile


class NdarrayDownsamplingImage(DownsamplingImage):
    """
    Down-samples a tiled source image whose tiles are numpy ndarray images.
    """

    def __init__(self,
                 source_image,
                 image_id=None,
                 tile_cache=None,
                 aggregator=aggregate_ndarray_first):
        """
        Constructor.
        :param source_image: a tiled source image (type TiledImage) whose source tiles must be PIL Images
        :param key: an optional key prefix string used for caching
        :param tile_cache: an optional tile cache of type Cache
        :param aggregator: an aggregator function which will be called like so:
                aggregator(downsampled_tile_00, downsampled_tile_01, downsampled_tile_10, downsampled_tile_11).
                see utils.downsample_ndarray() function
        """
        super().__init__(source_image, image_id=image_id, tile_cache=tile_cache)
        self._aggregator = aggregator

    def aggregate_and_stitch_source_tiles(self, source_tiles, target_size, target_positions):
        prototype_tile = source_tiles[0]
        agg_tiles = [downsample_ndarray(source_tile, aggregator=self._aggregator) for source_tile in source_tiles]
        target_shape = list(prototype_tile.shape)
        target_shape[-1] = target_size[0]
        target_shape[-2] = target_size[1]
        if np.ma.is_masked(prototype_tile):
            target_tile = np.ma.empty_like(prototype_tile)
        else:
            target_tile = np.empty_like(prototype_tile)
        for i in range(len(agg_tiles)):
            agg_x = target_positions[i][0]
            agg_y = target_positions[i][1]
            agg_tile = agg_tiles[i]
            agg_h, agg_w = agg_tile.shape[-2], agg_tile.shape[-1]
            # print('agg_tile h, w: ', agg_h, agg_w)
            target_tile[..., agg_y:agg_y + agg_h, agg_x:agg_x + agg_w] = agg_tile
        return target_tile


class FastNdarrayDownsamplingImage(OpImage):
    """
    Down-samples a tiled source image whose tiles are numpy ndarray images.
    """

    def __init__(self,
                 array,
                 tile_size,
                 z_index,
                 num_levels,
                 tile_cache=None):
        """
        Constructor.
        :param source_image: a tiled source image (type TiledImage) whose source tiles must be PIL Images
        :param key: an optional key prefix string used for caching
        :param tile_cache: an optional tile cache of type Cache
        """
        zoom = 1 << (num_levels - z_index - 1)
        image_id = '%s-L%d' % (uuid.uuid4(), z_index)
        source_width, source_height = array.shape[-1], array.shape[-2]
        width, height = source_width // zoom, source_height // zoom
        tile_width, tile_height = tile_size
        num_tiles_x, num_tiles_y = cardinal_div_round(width, tile_width), \
                                   cardinal_div_round(height, tile_height)
        super().__init__((width, height),
                         tile_size=(tile_width, tile_height),
                         num_tiles=(num_tiles_x, num_tiles_y),
                         mode=str(array.dtype),
                         format=None,
                         image_id=image_id,
                         tile_cache=tile_cache)
        self._array = array
        self._z_index = z_index
        self._zoom = zoom

    def compute_tile(self, tile_x, tile_y, rectangle):
        x, y, w, h = rectangle
        zoom = self._zoom
        x *= zoom
        y *= zoom
        w *= zoom
        h *= zoom

        tile = self._array[..., y:y + h:zoom, x:x + w:zoom]

        actual_tile_size = tile.shape[-1], tile.shape[-2]

        # todo: ensure that our tile size is w x h: resize and fill in background value.
        # For time being raise error
        assert self.tile_size == actual_tile_size, \
            "unexpected tile size: expected %s, but got %s" % (self.tile_size, actual_tile_size)

        return tile


class ImagePyramid:
    """
    A stack of tiled images (see TileImage) that form a quadtree image pyramid with increasing levels of detail.
    Level 0 represents the lowest resolution.
    The level of detail (image resolution) increases by a factor of two between any two, subsequent levels.
    The tile sizes for each level are the same.
    """

    @staticmethod
    def create_from_image(source_image,
                          level_image_factory,
                          num_level_zero_tiles=None,
                          num_levels=None,
                          **kwargs):

        """
        Create an image pyramid build from a single, max-resolution source image of type TiledImage.
        The given source image will be returned for highest resolution level in the pyramid.
        Other level images are created from the given level_image_factory function.

        :param source_image: the high-resolution source image, see TiledImage interface
        :param level_image_factory: creates the other level images, called as so:
               level_images[z_index] = level_image_factory(source_image, level_images[z_index+1], z_index, **kwargs)
        :param num_level_zero_tiles: a tuple (num_level_zero_tiles_x, num_level_zero_tiles_y)
        :param num_levels: number of levels
        :param kwargs: keyword arguments passed to the level_image_factory function
        :return: a new ImagePyramid instance
        """
        _, _, num_level_zero_tiles, num_levels = ImagePyramid.compute_layout(max_size=source_image.size,
                                                                             tile_size=source_image.tile_size,
                                                                             num_level_zero_tiles=num_level_zero_tiles,
                                                                             num_levels=num_levels)
        level_images = [None] * num_levels
        z_index_max = num_levels - 1
        level_images[z_index_max] = source_image
        level_image = source_image
        for i in range(1, num_levels):
            z_index = z_index_max - i
            level_images[z_index] = level_image = level_image_factory(source_image, level_image,
                                                                      z_index, num_levels, **kwargs)
        return ImagePyramid(num_level_zero_tiles, source_image.tile_size, level_images)

    @staticmethod
    def create_from_array(array,
                          tile_size=None,
                          num_level_zero_tiles=None,
                          num_levels=None,
                          **kwargs):

        """
        Create an image pyramid build from a numpy-like array using nearest neighbor resampling.
        This is a fast pyramid exploiting the array's underlying slicing capabilities.
        For example, if array is a H5Py dataset object, the created pyramid will take advantage of
        the HDF-5 libraries's slicing.

        :param array: numpy-like array that supports stepping in it's subscript operator, e.g.
                      array[..., y::step, x:step]
        :param tile_size: a tuple (tile_width, tile_height)
        :param num_level_zero_tiles: a tuple (num_level_zero_tiles_x, num_level_zero_tiles_y)
        :param num_levels: number of levels
        :return: a new ImagePyramid instance
        """
        max_size, tile_size, \
        num_level_zero_tiles, num_levels = ImagePyramid.compute_layout(array=array,
                                                                       tile_size=tile_size,
                                                                       num_level_zero_tiles=num_level_zero_tiles,
                                                                       num_levels=num_levels)
        level_images = [None] * num_levels
        for i in range(0, num_levels):
            z_index = num_levels - 1 - i
            level_images[z_index] = FastNdarrayDownsamplingImage(array, tile_size, z_index, num_levels, **kwargs)
        return ImagePyramid(num_level_zero_tiles, tile_size, level_images)

    @staticmethod
    def compute_layout(max_size=None,
                       array=None,
                       tile_size=None,
                       int_div=True,
                       num_level_zero_tiles=None,
                       num_levels=None):
        """
        Compute a suitable pyramid layout.

        :param max_size: maximum image size as (width, height)
        :param tile_size: optional tile size (tile_width, tile_height)
        :param int_div: mux_size must be integer-divisible by tile size
        :param num_level_zero_tiles: optional number of level zero tiles
        :param num_levels: optional number of levels
        :return: pyramid layout as tuple (max_size, tile_size, num_level_zero_tiles, num_levels)
        """
        if array is not None:
            size = array.shape[-1], array.shape[-2]
            if not max_size:
                max_size = size
            elif max_size != size:
                raise ValueError('incompatible max_size and array values')
            if not tile_size:
                if hasattr(array, 'chunks') and array.chunks is not None:
                    chunk_width, chunk_height = array.chunks[-1], array.chunks[-2]
                else:
                    chunk_width, chunk_height = None, None
                tile_size = (compute_tile_size(max_size[0], chunk_size=chunk_width, int_div=int_div),
                             compute_tile_size(max_size[1], chunk_size=chunk_height, int_div=int_div))
        if not max_size:
            raise ValueError('missing max_size value')
        max_width, max_height = max_size
        if not tile_size:
            tile_size = (compute_tile_size(max_width, int_div=int_div),
                         compute_tile_size(max_height, int_div=int_div))
        tile_width, tile_height = tile_size
        if not num_level_zero_tiles:
            num_level_zero_tiles = cardinal_div_round(max_width, max_height), \
                                   cardinal_div_round(max_height, max_width)
        if not num_levels:
            num_levels = 1
            num_tiles_x = num_level_zero_tiles[0]
            num_tiles_y = num_level_zero_tiles[1]
            while True:
                w = num_tiles_x * tile_width
                h = num_tiles_y * tile_height
                if w >= max_width and h >= max_height:
                    break
                num_tiles_x *= 2
                num_tiles_y *= 2
                num_levels += 1
        return max_size, tile_size, num_level_zero_tiles, num_levels

    def __init__(self, num_level_zero_tiles, tile_size, level_images):
        self._num_level_zero_tiles_x = num_level_zero_tiles[0]
        self._num_level_zero_tiles_y = num_level_zero_tiles[1]
        self._tile_width = tile_size[0]
        self._tile_height = tile_size[1]
        self._num_levels = len(level_images)
        self._level_images = list(level_images)

    @property
    def num_level_zero_tiles(self):
        return self._num_level_zero_tiles_x, self._num_level_zero_tiles_y

    @property
    def tile_size(self):
        return self._tile_width, self._tile_height

    @property
    def num_levels(self):
        return self._num_levels

    def get_level_image(self, z_index):
        return self._level_images[z_index]

    def get_tile(self, tile_x, tile_y, z_index):
        level_image = self._level_images[z_index]
        return level_image.get_tile(tile_x, tile_y)

    def dispose(self):
        for level_image in self._level_images:
            level_image.dispose()

    def apply(self, level_mapper, *args, **kwargs):
        return ImagePyramid(self.num_level_zero_tiles,
                            self.tile_size,
                            [level_mapper(level_image, *args, **kwargs) for level_image in self._level_images])


def create_pil_downsampling_image(source_image, higher_level_image, z_index, num_levels, **kwargs):
    image_id = '%s-L%d' % (source_image.id, z_index)
    return PilDownsamplingImage(higher_level_image, image_id=image_id, **kwargs)


def create_ndarray_downsampling_image(source_image, higher_level_image, z_index, num_levels, **kwargs):
    image_id = '%s-L%d' % (source_image.id, z_index)
    return NdarrayDownsamplingImage(higher_level_image, image_id=image_id, **kwargs)
