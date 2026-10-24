# PaperTrail Link Check

A small, standard-library-only checker for local links in Markdown files.

```console
python -m linkcheck README.md
python -m linkcheck docs/guide.md
python -m unittest discover -s tests -v
```

[Usage guide](docs/guide.md)

The command exits with `0` when all local targets exist, `1` when a target
is missing, and `2` when an input document cannot be read. HTTP, HTTPS,
mailto links and fragment-only links are skipped. A fragment on a local
link is ignored when checking whether the target file exists. Percent-encoded
paths such as `release%20notes.md` are supported.

This small tool handles simple inline links. It does not validate remote
URLs or heading anchors, or implement the complete Markdown grammar.
