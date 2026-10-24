"""Behavioral checks for the link checker; each case owns a temporary project."""

import os
from pathlib import Path
import shutil
import unittest
import uuid

from linkcheck import check_file


class LinkCheckerTests(unittest.TestCase):
    def setUp(self):
        self.previous_cwd = Path.cwd()
        temporary_root = Path(__file__).resolve().parents[1] / ".test-tmp"
        temporary_root.mkdir(exist_ok=True)
        self.root = temporary_root / ("case-" + uuid.uuid4().hex)
        self.root.mkdir()
        self.addCleanup(self.clean_project)
        self.addCleanup(os.chdir, self.previous_cwd)
        os.chdir(self.root)

    def clean_project(self):
        temporary_root = Path(__file__).resolve().parents[1] / ".test-tmp"
        if self.root.parent.resolve() != temporary_root.resolve():
            raise RuntimeError("Refusing to clean a directory outside the test project")
        shutil.rmtree(self.root)

    def write(self, path, text="example\n"):
        destination = self.root / path
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(text, encoding="utf-8")
        return destination

    def test_root_document_can_link_to_a_subdirectory(self):
        self.write("docs/guide.md")
        document = self.write("README.md", "[Guide](docs/guide.md)")
        self.assertTrue(check_file(document)[0].exists)

    def test_nested_document_resolves_a_sibling(self):
        self.write("docs/intro.md")
        document = self.write("docs/guide.md", "[Introduction](intro.md)")
        self.assertTrue(check_file(document)[0].exists)

    def test_nested_document_resolves_its_parent(self):
        name = self.root.name + "-overview.md"
        self.write(name)
        document = self.write("docs/guide.md", f"[Project](../{name})")
        self.assertTrue(check_file(document)[0].exists)

    def test_file_in_cwd_cannot_mask_a_missing_sibling(self):
        self.write("intro.md")
        document = self.write("docs/guide.md", "[Introduction](intro.md)")
        self.assertFalse(check_file(document)[0].exists)

    def test_missing_target_is_reported(self):
        document = self.write("README.md", "[Missing](missing.md)")
        results = check_file(document)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].target, "missing.md")
        self.assertFalse(results[0].exists)

    def test_external_urls_and_fragment_only_links_are_skipped(self):
        document = self.write("README.md", (
            "[Docs](https://example.com/docs)\n"
            "[HTTP](http://example.com)\n"
            "[Email](mailto:demo@example.invalid)\n"
            "[Section](#installation)\n"
        ))
        self.assertEqual(check_file(document), [])

    def test_local_fragment_does_not_change_the_file_target(self):
        self.write("docs/guide.md")
        document = self.write("README.md", "[Install](docs/guide.md#installation)")
        self.assertTrue(check_file(document)[0].exists)

    def test_percent_encoded_spaces_are_supported(self):
        self.write("release notes.md")
        document = self.write("README.md", "[Release notes](release%20notes.md)")
        self.assertTrue(check_file(document)[0].exists)


if __name__ == "__main__":
    unittest.main()
