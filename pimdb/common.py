import csv
import gzip
import json
import logging
import os.path
import time
from enum import Enum
from typing import Optional, Generator, Dict, Callable, Tuple

import requests

_MEGABYTE = 1048576

#: Logger for all output of the pimdb module.
log = logging.getLogger("pimdb")


class PimdbError(Exception):
    """Error representing that something went wrong during an pimdb operation."""

    pass


class PimdbTsvError(Exception):
    def __init__(self, path: str, row_number: int, base_message: str):
        self.path = path
        self.row_number = row_number
        self.message = f"{os.path.basename(self.path)} ({row_number}: {base_message}"


class ImdbDataset(Enum):
    """Names of all IMDb datasets available."""

    NAME_BASICS = "name.basics"
    TITLE_AKAS = "title.akas"
    TITLE_BASICS = "title.basics"
    TITLE_CREW = "title.crew"
    TITLE_PRINCIPALS = "title.principals"
    TITLE_RATINGS = "title.ratings"

    @property
    def filename(self):
        """
        The compressed file name for the URL, for example:

        >>> ImdbDataset("name.basics").filename
        'name.basics.tsv.gz'
        """
        return f"{self.value}.tsv.gz"

    @property
    def table_name(self):
        """
        Name for use in SQL tables, for example:

        >>> ImdbDataset("name.basics").table_name
        'name_basics'
        """
        return f"{self.value}".replace(".", "_")


class ReportTable(Enum):
    ALIAS_TYPE = "alias_type"
    CATEGORY = "category"
    CHARACTER = "character"
    GENRE = "genre"
    NAME = "name"
    NAME_TO_PROFESSION = "name_to_profession"
    PRINCIPAL_TO_CHARACTER = "principal_to_character"
    PROFESSION = "profession"
    TITLE = "title"
    TITLE_TO_DIRECTOR = "title_to_director"
    TITLE_TO_WRITER = "title_to_writer"
    TITLE_TO_PRINCIPAL = "title_to_principal"
    TITLE_TO_TYPE = "title_to_title_type"
    TITLE_TYPE = "title_type"


#: Names of all available IMDb datasets.
IMDB_DATASET_NAMES = [dataset.value for dataset in ImdbDataset]
IMDB_DATASET_TO_KEY_COLUMNS_MAP = {
    ImdbDataset.NAME_BASICS: ["nconst"],
    ImdbDataset.TITLE_AKAS: ["titleId", "ordering"],
    ImdbDataset.TITLE_BASICS: ["tconst"],
    ImdbDataset.TITLE_CREW: ["tconst"],
    ImdbDataset.TITLE_PRINCIPALS: ["nconst", "tconst"],
    ImdbDataset.TITLE_RATINGS: ["tconst"],
}


_DOWNLOAD_BUFFER_SIZE = 8192


class Settings:
    def __init__(self, data_folder: Optional[str] = None):
        self._data_folder = data_folder if data_folder is not None else ".pimdb"

    def pimdb_path(self, relative_path: str) -> str:
        """Path to a file or folder inside the pimdb data folder."""
        return os.path.join(self._data_folder, relative_path)


class LastModifiedMap:
    def __init__(self, last_modified_map_path: str):
        self._last_modified_map_path = last_modified_map_path
        self._url_to_last_modified_map = {}
        try:
            log.debug('reading "last modified" map from "%s"', self._last_modified_map_path)
            with open(self._last_modified_map_path, encoding="utf-8") as last_modified_file:
                self._url_to_last_modified_map = json.load(last_modified_file)
        except FileNotFoundError:
            # If we never cached anything before, just move on.
            log.debug('cannot find last modified map "%s", enforcing downloads', self._last_modified_map_path)
            pass
        except Exception as error:
            log.warning(
                'cannot process last modified map "%s", enforcing downloads: %s', self._last_modified_map_path, error
            )

    def is_modified(self, url: str, current_last_modified: str) -> bool:
        previous_last_modified = self._url_to_last_modified_map.get(url)
        log.debug(
            'checking last modified: previous=%r, current=%r, url="%s"',
            previous_last_modified,
            current_last_modified,
            url,
        )
        return current_last_modified != previous_last_modified

    def update(self, url: str, last_modified: str) -> None:
        self._url_to_last_modified_map[url] = last_modified

    def write(self) -> None:
        with open(self._last_modified_map_path, "w", encoding="utf-8") as last_modified_file:
            json.dump(self._url_to_last_modified_map, last_modified_file)


def download_imdb_dataset(imdb_dataset: ImdbDataset, target_path: str, only_if_newer: bool = True) -> None:
    source_url = f"https://datasets.imdbws.com/{imdb_dataset.filename}"
    last_modified_storage_path = os.path.join(os.path.dirname(target_path), ".pimdb_last_modified.json")
    last_modified_map = LastModifiedMap(last_modified_storage_path) if only_if_newer else None

    with requests.get(source_url, stream=True) as response:
        response.raise_for_status()
        if only_if_newer:
            current_last_modified = response.headers.get("last-modified")
            has_to_be_downloaded = last_modified_map.is_modified(source_url, current_last_modified)
        else:
            has_to_be_downloaded = True

        if has_to_be_downloaded:
            megabyte_to_download = int(response.headers.get("content-length", "0")) / _MEGABYTE
            length_text = f"{megabyte_to_download:.1f} MB " if megabyte_to_download > 0 else ""
            log.info('downloading %s"%s" to "%s"', length_text, source_url, target_path)
            with open(target_path, "wb") as target_file:
                for chunk in response.iter_content(chunk_size=_DOWNLOAD_BUFFER_SIZE):
                    if chunk:  # filter out keep-alive new chunks
                        target_file.write(chunk)
            if only_if_newer:
                last_modified_map.update(source_url, current_last_modified)
                last_modified_map.write()
        else:
            log.info('dataset "%s" is up to date, skipping download of "%s"', imdb_dataset.value, source_url)


class GzippedTsvReader:
    def __init__(
        self,
        gzipped_tsv_path: str,
        key_columns: Tuple[str],
        indicate_progress: Optional[Callable[[int, int], None]] = None,
        seconds_between_progress_update: float = 3.0,
    ):
        self._gzipped_tsv_path = gzipped_tsv_path
        self._row_number = None
        self._key_columns = key_columns
        self._duplicate_count = None
        self._indicate_progress = indicate_progress
        self._seconds_between_progress_update = seconds_between_progress_update

    @property
    def gzipped_tsv_path(self) -> str:
        return self._gzipped_tsv_path

    @property
    def row_number(self) -> int:
        assert self._row_number is not None
        return self._row_number

    @property
    def location(self) -> str:
        row_number_text = f" ({self.row_number})" if self.row_number is not None else ""
        return f"{os.path.basename(self.gzipped_tsv_path)}{row_number_text}"

    @property
    def duplicate_count(self) -> int:
        return self._duplicate_count

    def column_names_to_value_maps(self) -> Generator[Dict[str, str], None, None]:
        log.info('processing IMDb dataset file "%s"', self.gzipped_tsv_path)
        with gzip.open(self.gzipped_tsv_path, "rt", encoding="utf-8", newline="") as tsv_file:
            last_progress_time = time.time()
            last_progress_row_number = None
            existing_keys = set()
            self._duplicate_count = 0
            self._row_number = 0
            tsv_reader = csv.DictReader(tsv_file, delimiter="\t", quoting=csv.QUOTE_NONE, strict=True)
            try:
                for result in tsv_reader:
                    self._row_number += 1
                    key = tuple(result[key_column] for key_column in self._key_columns)
                    if key not in existing_keys:
                        existing_keys.add(key)
                        yield result
                    else:
                        log.debug("%s: ignoring duplicate %s=%s", self.location, self._key_columns, key)
                        self._duplicate_count += 1
                    if self._indicate_progress is not None:
                        current_time = time.time()
                        if current_time - last_progress_time > self._seconds_between_progress_update:
                            self._indicate_progress(self.row_number, self.duplicate_count)
                            last_progress_time = current_time
                if self._duplicate_count != last_progress_row_number and self._indicate_progress is not None:
                    self._indicate_progress(self.row_number, self.duplicate_count)
            except csv.Error as error:
                raise PimdbTsvError(self.gzipped_tsv_path, self.row_number, str(error))
