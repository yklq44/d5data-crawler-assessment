"""Checkpointing and CSV export."""

from __future__ import annotations

import csv
import json
import logging
import threading
from collections.abc import Iterable
from pathlib import Path

from .models import CSV_COLUMNS, BlogPost

log = logging.getLogger(__name__)


class Checkpoint:
    """Append-only JSON Lines file with every finished post.

    A crawl that is interrupted (Ctrl+C, network loss, crash) restarts where it
    stopped: posts already in the file are not downloaded again. Thread-safe.
    """

    def __init__(self, path: Path, resume: bool = True) -> None:
        self.path = path
        self.posts: dict[str, BlogPost] = {}
        self._lock = threading.Lock()
        path.parent.mkdir(parents=True, exist_ok=True)
        if resume and path.exists():
            self._load()
        else:
            path.unlink(missing_ok=True)
        self._file = path.open('a', encoding='utf-8', newline='\n')

    def _load(self) -> None:
        with self.path.open(encoding='utf-8') as file:
            for number, line in enumerate(file, start=1):
                try:
                    post = BlogPost.from_json(json.loads(line))
                except (ValueError, TypeError):
                    log.warning('%s:%d: skipping unreadable line', self.path, number)
                    continue
                self.posts[post.url] = post
        if self.posts:
            log.info('Resuming: %d posts already in %s', len(self.posts), self.path)

    def __contains__(self, url: str) -> bool:
        return url in self.posts

    def __len__(self) -> int:
        return len(self.posts)

    def add(self, post: BlogPost) -> None:
        line = json.dumps(post.to_json(), ensure_ascii=False)
        with self._lock:
            self._file.write(line + '\n')
            self._file.flush()
            self.posts[post.url] = post

    def close(self) -> None:
        self._file.close()


def write_csv(posts: Iterable[BlogPost], path: Path) -> int:
    """Write posts in listing order; returns the number of rows.

    UTF-8 with a BOM so Excel detects the encoding. Cells may contain newlines
    (article paragraphs, one image URL per line); they are quoted per RFC 4180.
    """
    rows = sorted(posts, key=lambda post: (post.page, post.position))
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_name(path.name + '.part')
    with partial.open('w', encoding='utf-8-sig', newline='') as file:
        writer = csv.DictWriter(file, fieldnames=CSV_COLUMNS)
        writer.writeheader()
        writer.writerows(post.to_row() for post in rows)
    partial.replace(path)
    return len(rows)


def read_csv(path: Path) -> list[dict[str, str]]:
    csv.field_size_limit(2**31 - 1)  # some posts are longer than the default 128 KiB
    with path.open(encoding='utf-8-sig', newline='') as file:
        return list(csv.DictReader(file))
