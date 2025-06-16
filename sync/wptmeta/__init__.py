from __future__ import annotations
import os
from abc import ABCMeta, abstractmethod
from collections import OrderedDict, namedtuple
from copy import deepcopy
import urllib.parse

import yaml

from typing import Any, Iterable, Iterator, Optional, SupportsIndex, overload

"""Module for interacting with a web-platform-tests metadata repository"""


class DeleteTrackingList(list["MetaEntry"]):
    """A list that holds a reference to any elements that are removed"""

    def __init__(self, *args: MetaEntry, **kwargs: Any) -> None:
        self._deleted: list[LinkState] = []
        super().__init__(*args, **kwargs)

    @overload
    def __setitem__(self, index: SupportsIndex, value: MetaEntry, /) -> None:
        ...
    @overload
    def __setitem__(self, index: slice[Any, Any, Any], value: Iterable[MetaEntry], /) -> None:
        ...
    def __setitem__(self, index: SupportsIndex | slice[Any, Any, Any], value: MetaEntry | Iterable[MetaEntry], /) -> None:
        self._dirty = True
        if isinstance(index, slice) and isinstance(value, Iterable):
            super().__setitem__(index, value)
        elif isinstance(index, SupportsIndex) and isinstance(value, MetaEntry):
            super().__setitem__(index, value)
        else:
            raise TypeError("Invalid index/value types")

    def __delitem__(self, index: SupportsIndex | slice[Any, Any, Any], /) -> None:
        if isinstance(index, slice):
            self._deleted.extend(item._initial_state for item in self[index]
                                 if item._initial_state is not None)
        else:
            _initial_state = self[index]._initial_state
            if _initial_state is not None:
                self._deleted.append(_initial_state)
        super().__delitem__(index)

    def pop(self, index: SupportsIndex = -1) -> MetaEntry:
        rv = super().pop(index)
        if rv._initial_state is not None:
            self._deleted.append(rv._initial_state)
        return rv

    def remove(self, item: MetaEntry) -> None:
        try:
            return super().remove(item)
        finally:
            _initial_state = item._initial_state
            if _initial_state is not None:
                self._deleted.append(_initial_state)


def parse_test(test_id: str) -> tuple[str, str]:
    id_parts = urllib.parse.urlsplit(test_id)
    dir_name, test_file = id_parts.path.rsplit("/", 1)
    if dir_name[0] == "/":
        dir_name = dir_name[1:]
    test_name = urllib.parse.urlunsplit(("", "", test_file, id_parts.query,
                                         id_parts.fragment))
    return dir_name, test_name


class Reader(metaclass=ABCMeta):
    """Class implementing read operations on paths"""

    @abstractmethod
    def read_path(self, rel_path: str) -> bytes:
        """Read the contents of `rel_path` as a bytestring

        :param rel_path` Relative path to read
        :returns: Bytes containing path contents
        """
        pass

    @abstractmethod
    def exists(self, rel_path: str) -> bool:
        """Determine if `rel_path` is a valid path

        :param rel_path` Relative path
        :returns: Boolean indicating if `rel_path` is a valid path"""
        pass

    @abstractmethod
    def walk(self, rel_path: str) -> Iterator[str]:
        """Iterator over all paths under rel_path containing an object

        :param rel_path` Relative path
        :returns: Iterator over path strings
        """
        pass


class Writer(metaclass=ABCMeta):
    """Class implementing write operations on paths"""

    @abstractmethod
    def write(self, rel_path: str, data: bytes) -> None:
        """Write `data` to the object at `rel_path`

        :param rel_path` Relative path to object
        :param data: Bytes containing data to write
        """
        pass


class FilesystemReader(Reader):
    """Reader implementation operating on filesystem files"""

    def __init__(self, root: str):
        self.root = root

    def read_path(self, rel_path: str) -> bytes:
        path = os.path.join(self.root, rel_path)
        with open(path, "rb") as f:
            return f.read()

    def exists(self, rel_path: str) -> bool:
        return os.path.exists(os.path.join(self.root, rel_path))

    def walk(self, rel_path: str) -> Iterator[str]:
        base = os.path.join(self.root, rel_path)
        for dir_path, dir_names, file_names in os.walk(base):
            if "META.yml" in file_names:
                yield os.path.relpath(dir_path, self.root)


class FilesystemWriter(Writer):
    """Writer implementation operating on filesystem files"""

    def __init__(self, root: str):
        self.root = root

    def write(self, rel_path: str, data: bytes) -> None:
        path = os.path.join(self.root, rel_path)
        with open(path, "wb") as f:
            f.write(data)


def metadata_directory(root: str) -> "WptMetadata":
    reader = FilesystemReader(root)
    writer = FilesystemWriter(root)
    return WptMetadata(reader, writer)


class WptMetadata:
    def __init__(self, reader: Reader, writer: Writer) -> None:
        """Object for working with a wpt-metadata tree

        :param reader: Object implementing Reader
        :param writer: Object implementing Writer"""
        self.reader = reader
        self.writer = writer
        self.loaded: dict[str, MetaFile] = {}

    def iter(self,
             test_id: str | None = None,
             product: str | None = None,
             subtest: str | None = None,
             status: str | None = None) -> Iterator[MetaEntry]:
        """Get the link metadata matching a specified set of conditions"""
        if test_id is None:
            dir_names = self.reader.walk("")
        else:
            assert test_id.startswith("/")
            dir_name, _ = parse_test(test_id)
            dir_names = iter([dir_name])
        for dir_name in dir_names:
            if dir_name not in self.loaded:
                self.loaded[dir_name] = MetaFile(self, dir_name)

            yield from self.loaded[dir_name].iter(product=product,
                                                  test_id=test_id,
                                                  subtest=subtest,
                                                  status=status)

    def iterlinks(self,
                  test_id: str | None = None,
                  product: str | None = None,
                  subtest: str | None = None,
                  status: str | None = None) -> Iterator[MetaLink]:
        """Get the link metadata matching a specified set of conditions"""
        for item in self.iter(test_id, product, subtest, status):
            if isinstance(item, MetaLink):
                yield item

    def iterlabels(self,
                   test_id: str | None = None,
                   product: str | None = None,
                   subtest: str | None = None,
                   status: str | None = None) -> Iterator[MetaLabel]:
        """Get the label metadata matching a specified set of conditions"""
        for item in self.iter(test_id, product, subtest, status):
            if isinstance(item, MetaLabel):
                yield item

    def write(self) -> list[str]:
        """Write any updated metadata to the metadata tree"""
        rv = []
        for meta_file in self.loaded.values():
            if meta_file.write():
                rv.append(meta_file.rel_path)
        return rv

    def append_link(self, url: str, product: str, test_id: str, subtest: str | None = None,
                    status: str | None = None) -> None:
        """Add a link to the metadata tree

        :param url: URL to link to
        :param product: Product for which the link is relevant
        :param test_id: Full test id for which the link is relevant
        :param subtest: Subtest for which the link is relevant or
                        None to apply to parent/all tests
        :param status: Result status for which the link is relevant or
                       None to apply to all statuses"""
        assert test_id.startswith("/")
        dir_name, test_name = parse_test(test_id)
        if dir_name not in self.loaded:
            self.loaded[dir_name] = MetaFile(self, dir_name)
        meta_file = self.loaded[dir_name]
        link = MetaLink(meta_file, test_id, url, product, subtest, status)
        meta_file.links.append(link)


class MetaFile:
    def __init__(self, owner: WptMetadata, dir_name: str) -> None:
        """Object representing a single META.yml file

        This uses an unusual algorithm for updated; first we reread
        the underlying data, then we apply changes that have been made
        locally on top of the re-read data. This allows the changes to
        be applied in the face of multiple writers without locking or
        generating conflicts that require human resolution. However
        the algorithm is not perfect; for example an entry that's
        deleted remotely may be readded if it has local modifications;
        that may or may not be correct depending on the situation.

        :param owner: The parent WptMetadata object
        :param dir_name: The relative path to the directory containing the
                         META.yml file
        """

        self.owner = owner
        self._file_data = None

        self.dir_name = dir_name
        dir_path = dir_name.replace("/", os.path.sep)
        self.rel_path = os.path.join(dir_path, "META.yml")

        self.links = DeleteTrackingList()

        self._file_data = self._load_file(self.rel_path)

        for link in self._file_data.get("links", []):
            for result in link.get("results", []):
                self.links.append(MetaEntry.from_file_data(self, link, result))

    def _load_file(self, rel_path: str) -> dict[str, Any]:
        if self.owner.reader.exists(rel_path):
            data = yaml.safe_load(self.owner.reader.read_path(rel_path))
        else:
            data = {}
        return data

    def iter(self,
             test_id: str | None = None,
             product: str | None = None,
             subtest: str | None = None,
             status: str | None = None) -> Iterator[MetaEntry]:
        """Iterator over all links in the file, filtered by arguments"""
        for item in self.links:
            if ((product is None or
                 (item.product is not None and
                  item.product.startswith(product))) and
                (test_id is None or
                 item.test_id == test_id) and
                (subtest is None or
                 getattr(item, "subtest", None) == subtest) and
                (status is None or
                 item.status == status)):
                yield item

    def write(self, reread: bool = True) -> bool:
        """Write the updated data to the underlying META.yml

        :param reread: Reread the underlying data before applying changes
        """
        data = self._get_data(reread)
        self._update_data(data)

        self.owner.writer.write(self.rel_path, yaml.safe_dump(data, encoding="utf8"))

        self._file_data = data
        self.links._deleted = []
        for link in self.links:
            link._initial_state = link.state
        return True

    def _get_data(self, reread: bool = True) -> dict[str, Any]:
        if not reread:
            assert self._file_data is not None
            data = deepcopy(self._file_data)
        else:
            data = self._load_file(self.rel_path)
        return data

    def _update_data(self,
                     data: dict[str, Any],
                     ) -> dict[str, Any]:
        links_by_state: dict[LinkState, LinkState] = OrderedDict()

        for item in data.get("links", []):
            label = item.get("label")
            url = item.get("url")
            product = item.get("product")
            for result in item["results"]:
                test_id = "/{}/{}".format(self.dir_name, result.get("test"))
                subtest = result.get("subtest")
                status = result.get("status")
                links_by_state[LinkState(label, url, product, test_id, subtest, status)] = (
                    LinkState(label, url, product, test_id, subtest, status))

        # Remove deletions first so that delete and readd works
        for item in self.links._deleted:
            if item in links_by_state:
                del links_by_state[item]

        for item in self.links:
            if item._initial_state in links_by_state:
                assert item._initial_state is not None
                links_by_state[item._initial_state] = item.state
            else:
                links_by_state[item.state] = item.state

        by_link: OrderedDict[tuple[str | None, str | None, str],
                             list[dict[str, Any]]] = OrderedDict()
        for link in links_by_state.values():
            result = {}
            test_id = link.test_id
            if test_id is not None:
                _, test = parse_test(test_id)
                result["test"] = test
            for prop in ["subtest", "status"]:
                value = getattr(link, prop)
                if value is not None:
                    result[prop] = value
            key = (link.label, link.url, link.product)
            if key not in by_link:
                by_link[key] = []
            by_link[key].append(result)

        links = []

        for (label, url, product), results in by_link.items():
            link_data: dict[str, str | list[dict[str, Any]]] = {"results": results}
            for link_key, value in [("label", label), ("url", url), ("product", product)]:
                if value is not None:
                    link_data[link_key] = value
            links.append(link_data)
        data["links"] = links

        return data


LinkState = namedtuple("LinkState", ["label", "url", "product", "test_id", "subtest", "status"])


class MetaEntry:
    __metaclass__ = ABCMeta

    def __init__(self, meta_file: MetaFile, test_id: str) -> None:
        """A single link object"""
        assert test_id.startswith("/")
        self.meta_file = meta_file
        self.test_id = test_id
        self._initial_state: LinkState | None = None
        self.product: Optional[str] = None
        self.status: Optional[str] = None

    @staticmethod
    def from_file_data(meta_file: MetaFile, link: dict[str, Any],
                       result: dict[str, str]) -> MetaLink | MetaEntry:
        if "label" in link:
            return MetaLabel.from_file_data(meta_file, link, result)
        elif "url" in link:
            return MetaLink.from_file_data(meta_file, link, result)
        else:
            raise ValueError("Unable to load metadata entry")

    def __repr__(self) -> str:
        return f"<{self.__class__.__name__} test_id: {self.test_id}>"

    @property
    @abstractmethod
    def state(self) -> LinkState:
        pass

    def delete(self) -> None:
        """Remove the link from the owning file"""
        self.meta_file.links.remove(self)


class MetaLabel(MetaEntry):
    def __init__(self,
                 meta_file: MetaFile,
                 test_id: str,
                 label: str,
                 url: str | None,
                 product: str | None = None,
                 status: str | None = None,
                 ) -> None:
        """A single link object"""
        super().__init__(meta_file, test_id)
        self.label = label
        self.url = url
        self.product = product
        self.status = status

    @classmethod
    def from_file_data(cls, meta_file: MetaFile, link: dict[str, Any],
                       result: dict[str, str]) -> MetaLabel:
        test_id = "/{}/{}".format(meta_file.dir_name, result["test"])
        label = link["label"]
        url = link.get("url")
        product = link.get("product")
        status = result.get("status")
        self = cls(meta_file, test_id, label, url, product, status)
        self._initial_state = self.state
        return self

    def __repr__(self) -> str:
        base = super().__repr__()
        return (f"{base[:-1]} label: {self.label} url: {self.url} "
                f"product: {self.product} status: {self.status}>")

    @property
    def state(self) -> LinkState:
        return LinkState(self.label,
                         self.url,
                         self.product,
                         self.test_id,
                         None,
                         self.status)


class MetaLink(MetaEntry):
    def __init__(self,
                 meta_file: MetaFile,
                 test_id: str,
                 url: str,
                 product: str | None,
                 subtest: str | None = None,
                 status: str | None = None,
                 ) -> None:
        """A single link object"""
        super().__init__(meta_file, test_id)
        self.url = url
        self.product = product
        self.subtest = subtest
        self.status = status

    @classmethod
    def from_file_data(cls, meta_file: MetaFile, link: dict[str, Any],
                       result: dict[str, str]) -> MetaLink:
        test_id = "/{}/{}".format(meta_file.dir_name, result["test"])
        url = link["url"]
        product = link.get("product")
        status = result.get("status")
        subtest = result.get("subtest")
        self = cls(meta_file, test_id, url, product, subtest, status)
        self._initial_state = self.state
        return self

    def __repr__(self) -> str:
        base = super().__repr__()
        return (f"{base[:-1]} url: {self.url} product: {self.product} "
                f"status: {self.status} subtest: {self.subtest}>")

    @property
    def state(self) -> LinkState:
        return LinkState(None,
                         self.url,
                         self.product,
                         self.test_id,
                         self.subtest,
                         self.status)
