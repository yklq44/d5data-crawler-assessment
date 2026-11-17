"""Records produced by the crawlers."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

REQUIRED_COLUMNS = (
    'Title',
    'Author',
    'Comment Count',
    'Time',
    'Article Content',
    'Images In Article',
)
CSV_COLUMNS = (*REQUIRED_COLUMNS, 'URL', 'Subtitle', 'Categories')


@dataclass(slots=True)
class Teaser:
    """A post as listed on https://mitadmissions.org/blogs/page/N/."""

    url: str
    title: str
    subtitle: str
    author: str
    date: str  # ISO date shown on the card, e.g. "2026-11-16"
    comment_count: int | None = None  # None until the Disqus count is known
    disqus_id: str | None = None  # set for posts that use Disqus comments


@dataclass(slots=True)
class Article:
    """Fields read from the post page itself."""

    url: str
    title: str
    subtitle: str
    author: str
    published: str  # ISO 8601 in the site's time zone (America/New_York)
    categories: list[str] = field(default_factory=list)
    content: str = ''
    images: list[str] = field(default_factory=list)


@dataclass(slots=True)
class BlogPost:
    """One CSV row: listing data (comment count, order) merged with the article."""

    url: str
    title: str
    subtitle: str
    author: str
    time: str
    comment_count: int | None
    categories: list[str]
    content: str
    images: list[str]
    page: int  # listing page and position on it, used to keep the site's order
    position: int

    @classmethod
    def merge(cls, teaser: Teaser, article: Article | None, page: int, position: int) -> BlogPost:
        """Combine both sources; without an article the listing data is kept."""
        if article is None:
            return cls(
                url=teaser.url,
                title=teaser.title,
                subtitle=teaser.subtitle,
                author=teaser.author,
                time=teaser.date,
                comment_count=teaser.comment_count,
                categories=[],
                content='',
                images=[],
                page=page,
                position=position,
            )
        return cls(
            url=teaser.url,
            title=article.title or teaser.title,
            subtitle=article.subtitle or teaser.subtitle,
            author=article.author or teaser.author,
            time=article.published or teaser.date,
            comment_count=teaser.comment_count,
            categories=article.categories,
            content=article.content,
            images=article.images,
            page=page,
            position=position,
        )

    def to_row(self) -> dict[str, str]:
        return {
            'Title': self.title,
            'Author': self.author,
            'Comment Count': '' if self.comment_count is None else str(self.comment_count),
            'Time': self.time,
            'Article Content': self.content,
            'Images In Article': '\n'.join(self.images),
            'URL': self.url,
            'Subtitle': self.subtitle,
            'Categories': '; '.join(self.categories),
        }

    def to_json(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> BlogPost:
        return cls(**data)
