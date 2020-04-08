import gzip

from pimdb.common import TsvDictWriter, GzippedTsvReader

from tests._common import output_path


def test_can_write_and_read_gzipped_tsv():
    target_path = output_path(f"{__name__}.csv.gz")
    rows_to_write = [{"name": "bob", "profession": "blacksmith"}, {"name": "alice", "profession": '"potter"'}]

    with gzip.open(target_path, "wt", encoding="utf-8", newline="") as target_file:
        tsv_writer = TsvDictWriter(target_file)
        for row_to_write in rows_to_write:
            tsv_writer.write(row_to_write)

    gzipped_tsv_reader = GzippedTsvReader(target_path, ("name",))
    rows_read = list(gzipped_tsv_reader.column_names_to_value_maps())

    assert rows_to_write == rows_read
