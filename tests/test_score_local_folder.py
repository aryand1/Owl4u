from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "batch"))

import score_local_folder as local  # noqa: E402


class LocalDiscoveryTests(unittest.TestCase):
    def test_discovers_ontology_xml_and_rejects_office_xml(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ontology = root / "example.xml"
            ontology.write_text(
                '<?xml version="1.0"?><rdf:RDF '
                'xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"/>',
                encoding="utf-8",
            )
            office = root / "xl" / "styles.xml"
            office.parent.mkdir()
            office.write_text(
                '<?xml version="1.0"?><styleSheet '
                'xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"/>',
                encoding="utf-8",
            )
            self.assertEqual([ontology.resolve()], local.discover_files(root))

    def test_keeps_ontology_specific_suffix_even_if_malformed(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "needs-repair.owl"
            path.write_text("not currently parseable", encoding="utf-8")
            self.assertTrue(local.is_ontology_candidate(path))


if __name__ == "__main__":
    unittest.main()
