import time
from abc import ABCMeta, abstractmethod
from threading import RLock


class CacheStore(metaclass=ABCMeta):
    """
    Represents a store to which cached values can be stored into and restored from.
    """

    @abstractmethod
    def store_value(self, key, value):
        """
        Store a value and return it's stored representation and size in any unit, e.g. in bytes.
        :param key: the key
        :param value: the value
        :return: a 2-element sequence containing the stored representation of the value and it's size
        """
        pass

    @abstractmethod
    def restore_value(self, key, stored_value):
        """
        Restore a vale from its stored representation.
        :param key: the key
        :param stored_value: the stored representation of the value
        :return: the item
        """
        pass

    @abstractmethod
    def discard_value(self, key, stored_value):
        """
        Discard a value from it's storage.
        :param key: the key
        :param stored_value: the stored representation of the value
        """
        pass


class MemoryCacheStore(CacheStore):
    """
    Simple memory store.
    """

    def store_value(self, key, value):
        """
        Return (value, 1).
        :param key: the key
        :param value: the value
        :return: (value, 1)
        """
        return value, 1

    def restore_value(self, key, stored_value):
        """
        Return stored_value.
        :param key: the key
        :param stored_value: the stored representation of the value
        :return: stored_value
        """
        return stored_value

    def discard_value(self, key, stored_value):
        """
        Do nothing.
        :param key: the key
        :param stored_value: the stored representation of the value
        """
        pass


# Discard Least Recently Used items first
POLICY_LRU = lambda item: item.access_time

# Discard Most Recently Used first
POLICY_MRU = lambda item: -item.access_time

# Discard Least Frequently Used first
POLICY_LFU = lambda item: item.access_count

# Discard items by Random Replacement
POLICY_RR = lambda item: item.access_count % 2

_T0 = time.clock()


class Cache:
    """
    An implementation of a cache.
    See https://en.wikipedia.org/wiki/Cache_algorithms
    """

    class Item:
        """
        Cache-private class representing an item in the cache.
        """

        def __init__(self):
            self.key = None
            self.stored_value = None
            self.stored_size = 0
            self.creation_time = 0
            self.access_time = 0
            self.access_count = 0

        def access(self):
            self.access_time = time.clock() - _T0
            self.access_count += 1

        def store(self, store, key, value):
            self.key = key
            self.access_count = 0
            self.access()
            stored_value, stored_size = store.store_value(key, value)
            self.stored_value = stored_value
            self.stored_size = stored_size

        def restore(self, store, key):
            self.access()
            return store.restore_value(key, self.stored_value)

        def discard(self, store, key):
            store.discard_value(key, self.stored_value)
            self.__init__()

    def __init__(self, store=MemoryCacheStore(), capacity=1000, threshold=0.75, policy=POLICY_LRU, parent_cache=None):
        """
        Constructor.
    
        :param policy: cache replacement policy: LRU, MRU, LFU, or RR
        :param store: the cache store, see CacheStore interface
        :param capacity: the size capacity in units used by the store's store() method
        :param threshold: a number greater than zero and less than one
        """
        self._store = store
        self._capacity = capacity
        self._threshold = threshold
        self._policy = policy
        self._parent_cache = parent_cache
        self._size = 0
        self._max_size = self._capacity * self._threshold
        self._item_dict = {}
        self._item_list = []
        self._lock = RLock()

    @property
    def policy(self):
        return self._policy

    @property
    def store(self):
        return self._store

    @property
    def capacity(self):
        return self._capacity

    @property
    def threshold(self):
        return self._threshold

    @property
    def size(self):
        return self._size

    @property
    def max_size(self):
        return self._max_size

    def get_value(self, key):
        self._lock.acquire()
        item = self._item_dict.get(key)
        value = None
        if item:
            value = item.restore(self._store, key)
        elif self._parent_cache:
            item = self._parent_cache.get_value(key)
            if item:
                value = item.restore(self._parent_cache.store, key)
        self._lock.release()
        return value

    def put_value(self, key, value):
        self._lock.acquire()
        if self._parent_cache:
            # remove value from parent cache, because this cache will now take over
            self._parent_cache.remove_value(key)
        item = self._item_dict.get(key)
        if item:
            self._remove_item(item)
            self._size -= item.stored_size
            item.discard(self._store, key)
        else:
            item = Cache.Item()
        item.store(self._store, key, value)
        if self._size + item.stored_size > self._max_size:
            self.trim(item.stored_size)
        self._size += item.stored_size
        self._add_item(item)
        self._lock.release()

    def remove_value(self, key):
        self._lock.acquire()
        if self._parent_cache:
            self._parent_cache.remove_value(key)
        item = self._item_dict.get(key)
        if item:
            self._remove_item(item)
            self._size -= item.stored_size
            item.discard(self._store, key)
        self._lock.release()

    def _add_item(self, item):
        self._item_dict[item.key] = item
        self._item_list.append(item)

    def _remove_item(self, item):
        self._item_dict.pop(item.key)
        self._item_list.remove(item)

    def trim(self, extra_size=0):
        self._lock.acquire()
        self._item_list.sort(key=self._policy)
        keys = []
        size = self._size
        max_size = self._max_size
        for item in self._item_list:
            if size + extra_size > max_size:
                keys.append(item.key)
                size -= item.stored_size
        self._lock.release()
        # release lock to give another thread a chance then require lock again
        self._lock.acquire()
        for key in keys:
            if self._parent_cache:
                # Before discarding item fully, put its value into the parent cache
                value = self.get_value(key)
                self.remove_value(key)
                if value:
                    self._parent_cache.put_value(key, value)
            else:
                self.remove_value(key)
        self._lock.release()

    def clear(self, clear_parent=True):
        self._lock.acquire()
        if self._parent_cache and clear_parent:
            self._parent_cache.clear(clear_parent)
        keys = list(self._item_dict.keys())
        self._lock.release()
        for key in keys:
            if self._parent_cache and not clear_parent:
                value = self.get_value(key)
                if value:
                    self._parent_cache.put_value(key, value)
            self.remove_value(key)
