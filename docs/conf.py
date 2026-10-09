"""Sphinx configuration for the documentation site (see CONTRIBUTING.md)."""

import importlib.metadata

project = "image-review"
author = "Judd Storrs"
release = importlib.metadata.version("image-review")

extensions = [
    "myst_parser",  # the pages are Markdown, as GitHub renders them
    "sphinx_copybutton",
]

# Give every heading down to ### an anchor, so `page.md#heading` links resolve
myst_heading_anchors = 3

exclude_patterns = ["_build"]

html_theme = "furo"
html_title = "image-review"
html_baseurl = "https://jstorrs.github.io/image-review/"
html_theme_options = {
    # The edit link in each page
    "source_repository": "https://github.com/jstorrs/image-review",
    "source_branch": "main",
    "source_directory": "docs/",
}
