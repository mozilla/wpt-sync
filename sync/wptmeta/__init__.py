from __future__ import annotations
import os
from abc import ABCMeta, abstractmethod
from collections import OrderedDict, namedtuple
from copy import deepcopy

import yaml
import six
from six.moves import urllib

from typing import Any, Dict, Iterator, List, Optional, Text, Tuple

"""Module for interacting with a web-platform-tests metadata repository"""


class DeleteTrackingList(list):
    """A list that holds a reference to any elements that are removed"""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self._deleted: List[Any] = []
        super().__init__(*args, **kwargs)

    def __setitem__(self, index, value):
        self._dirty = True
        super().__setitem__(index, value)

    def __setslice__(self, index0, index1, value):
        self.deleted.extend(self[index0:index1])
        super().__setslice__(index0, index1, value)

    def __delitem__(self, index):
        self.deleted.append(self[index]._initial_state)
        super().__delitem__(index)

    def __delslice__(self, index0, index1):
        self.deleted.extend(self[index0:index1])
        super().__delslice__(index0, index1)

    def pop(self):
        rv = super().pop()
        self._deleted.append(rv)
        return rv

    def remove(self, item: Any) -> Any:
        try:
            return super().remove(item)
        finally:
            self._deleted.append(item)


def parse_test(test_id: Text) -> Tuple[Text, Text]:
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
    def read_path(self, rel_path: Text) -> bytes:
        """Read the contents of `rel_path` as a bytestring

        :param rel_path` Relative path to read
        :returns: Bytes containing path contents
        """
        pass

    @abstractmethod
    def exists(self, rel_path: Text) -> bool:
        """Determine if `rel_path` is a valid path

        :param rel_path` Relative path
        :returns: Boolean indicating if `rel_path` is a valid path"""
        pass

    @abstractmethod
    def walk(self, rel_path: Text) -> Iterator[Text]:
        """Iterator over all paths under rel_path containing an object

        :param rel_path` Relative path
        :returns: Iterator over path strings
        """
        pass


class Writer(metaclass=ABCMeta):
    """Class implementing write operations on paths"""

    @abstractmethod
    def write(self, rel_path: Text, data: bytes) -> None:
        """Write `data` to the object at `rel_path`

        :param rel_path` Relative path to object
        :param data: Bytes containing data to write
        """
        pass


class FilesystemReader(Reader):
    """Reader implementation operating on filesystem files"""

    def __init__(self, root):
        self.root = root

    def read_path(self, rel_path):
        path = os.path.join(self.root, rel_path)
        with open(path) as f:
            return f.read()

    def exists(self, rel_path):
        return os.path.exists(os.path.join(self.root, rel_path))

    def walk(self, rel_path):
        base = os.path.join(self.root, rel_path)
        for dir_path, dir_names, file_names in os.walk(base):
            if "META.yml" in file_names:
                yield os.path.relpath(dir_path, self.root)


class FilesystemWriter(Writer):
    """Writer implementation operating on filesystem files"""

    def __init__(self, root):
        self.root = root

    def write(self, rel_path, data):
        path = os.path.join(self.root, rel_path)
        with open(path, "w") as f:
            return f.write(data)


def metadata_directory(root):
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
        self.loaded: Dict[Text, MetaFile] = {}

    def iterlinks(self,
                  test_id: Text,
                  product: Optional[Text] = None,
                  subtest: Optional[Text] = None,
                  status: Optional[Text] = None,
                  ) -> Iterator[MetaLink]:
        """Get the metadata matching a specified set of conditions"""
        if test_id is None:
            dir_names = self.reader.walk("")
        else:
            assert test_id.startswith("/")
            dir_name, _ = parse_test(test_id)
            dir_names = [dir_name]
        for dir_name in dir_names:
            if dir_name not in self.loaded:
                self.loaded[dir_name] = MetaFile(self, dir_name)

            yield from self.loaded[dir_name].iterlinks(product=product,
                                                       test_id=test_id,
                                                       subtest=None,
                                                       status=None)

    def write(self) -> List[Text]:
        """Write any updated metadata to the metadata tree"""
        rv = []
        for meta_file in self.loaded.values():
            if meta_file.write():
                rv.append(meta_file.rel_path)
        return rv

    def append_link(self, url: Text, product: Text, test_id: Text, subtest: Optional[Text] = None,
                    status: Optional[Text] = None) -> None:
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
        link = MetaLink(meta_file, url, product, test_id, subtest, status)
        meta_file.links.append(link)


class MetaFile:
    def __init__(self, owner: WptMetadata, dir_name: Text) -> None:
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
                self.links.append(MetaLink.from_file_data(self, link, result))

    def _load_file(self, rel_path: Text) -> Dict[Text, Any]:
        if self.owner.reader.exists(rel_path):
            data = yaml.safe_load(self.owner.reader.read_path(rel_path))
        else:
            data = {}
        return data

    def iterlinks(self,
                  product: Optional[Text] = None,
                  test_id: Optional[Text] = None,
                  subtest: Optional[Text] = None,
                  status: Optional[Text] = None,
                  ) -> Iterator[MetaLink]:
        """Iterator over all links in the file, filtered by arguments"""
        for item in self.links:
            if ((product is None or
                 item.product.startswith(product)) and
                (test_id is None or
                 item.test_id == test_id) and
                (subtest is None or
                 item.subtest == subtest) and
                (status is None or
                 item.status == status)):
                yield item

    def write(self, reread: bool = True) -> bool:
        """Write the updated data to the underlying META.yml

        :param reread: Reread the underlying data before applying changes
        """
        data = self._get_data(reread)
        self._update_data(data)

        self.owner.writer.write(self.rel_path, yaml.safe_dump(data))

        self._file_data = data
        self.links._deleted = []
        for link in self.links:
            link._initial_state = link.state
        return True

    def _get_data(self, reread: bool = True) -> Dict[Text, Any]:
        if not reread:
            assert self._file_data is not None
            data = deepcopy(self._file_data)
        else:
            data = self._load_file(self.rel_path)
        return data

    def _update_data(self,
                     data: Dict[Text, Any],
                     ) -> Dict[Text, Any]:
        links_by_state = OrderedDict()

        for item in data.get("links", []):
            url = item.get("url")
            product = item.get("product")
            for result in item["results"]:
                test_id = "/{}/{}".format(self.dir_name, result.get("test"))
                subtest = result.get("subtest")
                status = result.get("status")
                links_by_state[LinkState(url, product, test_id, subtest, status)] = (
                    LinkState(url, product, test_id, subtest, status))

        # Remove deletions first so that delete and readd works
        for item in self.links._deleted:
            if item._initial_state in links_by_state:
                del links_by_state[item._initial_state]

        for item in self.links:
            if item._initial_state in links_by_state:
                links_by_state[item._initial_state] = item.state
            else:
                links_by_state[item.state] = item.state

        by_link: OrderedDict[Tuple[Text, Text], List[Dict[Text, Any]]] = OrderedDict()
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
            key = (link.url, link.product)
            if key not in by_link:
                by_link[key] = []
            by_link[key].append(result)

        links = []

        for (url, product), results in by_link.items():
            links.append({"url": url,
                          "product": product,
                          "results": results})
        data["links"] = links

        return data


LinkState = namedtuple("LinkState", ["url", "product", "test_id", "subtest", "status"])


class MetaLink:
    def __init__(self,
                 meta_file: MetaFile,
                 url: Text,
                 product: Optional[Text],
                 test_id: Text,
                 subtest: Optional[Text] = None,
                 status: Optional[Text] = None,
                 ) -> None:
        """A single link object"""
        assert test_id.startswith("/")
        self.meta_file = meta_file
        self.url = url
        self.product = product
        self.test_id = test_id
        self.subtest = subtest
        self.status = status
        self._initial_state: Optional[LinkState] = None

    @classmethod
    def from_file_data(cls, meta_file: MetaFile, link: Dict[Text, Any],
                       result: Dict[Text, Text]) -> MetaLink:
        url = link["url"]
        product = link.get("product")
        test_id = "/{}/{}".format(meta_file.dir_name, result["test"])
        status = result.get("status")
        subtest = result.get("subtest")
        self = cls(meta_file, url, product, test_id, subtest, status)
        self._initial_state = self.state
        return self

    @property
    def state(self) -> LinkState:
        return LinkState(self.url,
                         self.product,
                         self.test_id,
                         self.subtest,
                         self.status)

    def __repr__(self) -> str:
        data = (self.__class__.__name__,) + self.state
        return six.ensure_str("<%s url:%s product:%s test:%s subtest:%s status:%s>" % data)

    def delete(self) -> None:
        """Remove the link from the owning file"""
        self.meta_file.links.remove(self)
