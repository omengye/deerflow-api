import logging
import os
import re
import subprocess
import sys
from urllib.parse import urljoin

from markdownify import markdownify as md
from readabilipy import simple_json_from_html_string

logger = logging.getLogger(__name__)


def _patch_readabilipy_for_windows() -> None:
    """Fix readabilipy's node/npm detection on Windows.

    readabilipy calls ``subprocess.run(["node", ...])`` / ``["npm", ...]``
    without ``shell=True``. On Windows ``npm`` is ``npm.cmd`` and Python's
    ``CreateProcess`` will not resolve ``.cmd`` extensions automatically,
    causing ``FileNotFoundError`` even when Node.js is correctly installed.
    The result is the spurious warnings:

        Warning: A working NPM installation was not found...
        Warning: node executable not found, reverting to pure-Python mode...

    This patch replaces ``have_node`` / ``have_npm`` in readabilipy with
    versions that resolve the executable via ``shutil.which`` (which honours
    PATHEXT on Windows) and call it by absolute path.
    """
    if sys.platform != "win32":
        return

    try:
        import shutil
        from readabilipy import simple_json as _rsj
        from readabilipy import utils as _rutils
    except ImportError:  # pragma: no cover - readabilipy is a hard dep
        return

    _PATCH_FLAG = "_deerflow_windows_patched"
    if getattr(_rsj, _PATCH_FLAG, False):
        return

    def _resolve(name: str) -> str | None:
        return shutil.which(name) or shutil.which(f"{name}.cmd") or shutil.which(f"{name}.exe")

    def have_node() -> bool:
        node_path = _resolve("node")
        if not node_path:
            return False
        try:
            cp = subprocess.run(
                [node_path, "-v"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
        except (FileNotFoundError, OSError):
            return False
        if cp.returncode != 0:
            return False
        try:
            major = int(cp.stdout.split(b".")[0].lstrip(b"v"))
        except (ValueError, IndexError):
            return False
        if major < 10:
            return False
        jsdir = os.path.join(os.path.dirname(_rsj.__file__), "javascript")
        node_modules = os.path.join(jsdir, "node_modules")
        if not os.path.exists(node_modules):
            _rutils.run_npm_install()
        return os.path.exists(node_modules)

    def have_npm() -> bool:
        npm_path = _resolve("npm")
        if not npm_path:
            return False
        try:
            cp = subprocess.run(
                [npm_path, "version"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
        except (FileNotFoundError, OSError):
            return False
        return cp.returncode == 0

    _rsj.have_node = have_node
    _rutils.have_npm = have_npm
    setattr(_rsj, _PATCH_FLAG, True)
    logger.debug("Applied readabilipy Windows compatibility patch.")


_patch_readabilipy_for_windows()


class Article:
    url: str

    def __init__(self, title: str, html_content: str):
        self.title = title
        self.html_content = html_content

    def to_markdown(self, including_title: bool = True) -> str:
        markdown = ""
        if including_title:
            markdown += f"# {self.title}\n\n"

        if self.html_content is None or not str(self.html_content).strip():
            markdown += "*No content available*\n"
        else:
            markdown += md(self.html_content)

        return markdown

    def to_message(self) -> list[dict]:
        image_pattern = r"!\[.*?\]\((.*?)\)"

        content: list[dict[str, str]] = []
        markdown = self.to_markdown()

        if not markdown or not markdown.strip():
            return [{"type": "text", "text": "No content available"}]

        parts = re.split(image_pattern, markdown)

        for i, part in enumerate(parts):
            if i % 2 == 1:
                image_url = urljoin(self.url, part.strip())
                content.append({"type": "image_url", "image_url": {"url": image_url}})
            else:
                text_part = part.strip()
                if text_part:
                    content.append({"type": "text", "text": text_part})

        # If after processing all parts, content is still empty, provide a fallback message.
        if not content:
            content = [{"type": "text", "text": "No content available"}]

        return content


class ReadabilityExtractor:
    def extract_article(self, html: str) -> Article:
        try:
            article = simple_json_from_html_string(html, use_readability=True)
        except (subprocess.CalledProcessError, FileNotFoundError) as exc:
            stderr = getattr(exc, "stderr", None)
            if isinstance(stderr, bytes):
                stderr = stderr.decode(errors="replace")
            stderr_info = f"; stderr={stderr.strip()}" if isinstance(stderr, str) and stderr.strip() else ""
            logger.warning(
                "Readability.js extraction failed with %s%s; "
                "install Node.js to enable high-quality extraction (falling back to pure-Python)",
                type(exc).__name__,
                stderr_info,
                exc_info=True,
            )
            article = simple_json_from_html_string(html, use_readability=False)

        html_content = article.get("content")
        if not html_content or not str(html_content).strip():
            html_content = "No content could be extracted from this page"

        title = article.get("title")
        if not title or not str(title).strip():
            title = "Untitled"

        return Article(title=title, html_content=html_content)
