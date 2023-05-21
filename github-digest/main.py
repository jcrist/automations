from __future__ import annotations

import datetime
import html
import os
import smtplib
import ssl

from collections import defaultdict
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from typing import Literal, Iterator

import msgspec
import requests


class Repo(msgspec.Struct, rename="camel", frozen=True):
    name_with_owner: str
    url: str


class Actor(msgspec.Struct):
    login: str


class Comment(msgspec.Struct, rename="camel"):
    updated_at: datetime.datetime


class Comments(msgspec.Struct, rename="camel", frozen=True):
    items: list[Comment] = []
    total_count: int = 0


class Item(msgspec.Struct, rename="camel", kw_only=True):
    type: Literal["Issue", "PullRequest", "Discussion"]
    author: Actor
    url: str
    created_at: datetime.datetime
    last_edited_at: datetime.datetime | None
    closed_at: datetime.datetime | None
    repo: Repo
    number: int
    title: str
    state: Literal["CLOSED", "MERGED", "OPEN", None] = None
    comments: Comments
    reviews: Comments = Comments()


class PageInfo(msgspec.Struct, rename="camel"):
    has_next_page: bool
    end_cursor: str | None = None


class SearchResults(msgspec.Struct):
    class _Search1(msgspec.Struct):
        class _Search2(msgspec.Struct, rename="camel"):
            page_info: PageInfo
            items: list[Item]

        search: _Search2

    data: _Search1


def fetch_recent_items(token: str, since: datetime.date) -> list[Item]:
    root = Path(__file__).absolute().parent
    with requests.Session() as session:
        search = f"msgspec updated:>={since}"
        items = []
        for file in ["issues.graphql", "discussions.graphql"]:
            with open(root / file, "r") as f:
                template = f.read()
            cursor = None
            while True:
                params = f'query: "{search}"'
                if cursor is not None:
                    params += f', after: "{cursor}"'

                query = template % params
                resp = session.post(  # type: ignore
                    "https://api.github.com/graphql",
                    headers={"Authorization": f"bearer {token}"},
                    json={"query": query},
                )
                msg = msgspec.json.decode(resp.content, type=SearchResults).data.search
                items.extend(msg.items)
                if msg.page_info.has_next_page:
                    cursor = msg.page_info.end_cursor
                else:
                    break
        return items


def select_interesting_items(items: list[Item], since: datetime.date) -> list[Item]:
    # Find everything interesting after 9 central (14 UTC) yesterday
    after = datetime.datetime.combine(
        since, datetime.time(14, tzinfo=datetime.timezone.utc)
    )
    IGNORE_AUTHORS = {
        "jcrist",
        "dependabot",
        "renovate",
        "phillip-ground",
        "ibis-squawk-bot",
    }
    IGNORE_REPOS = {
        "jcrist/msgspec",
        "conda-forge/msgspec-feedstock",
        "NixOS/nixpkgs",
    }
    return [
        item
        for item in items
        if (
            item.author.login not in IGNORE_AUTHORS
            and item.repo.name_with_owner not in IGNORE_REPOS
            and (
                item.created_at >= after
                or (item.last_edited_at is not None and item.last_edited_at >= after)
                or (item.closed_at is not None and item.closed_at >= after)
                or (item.comments.items and item.comments.items[0].updated_at >= after)
                or (item.reviews.items and item.reviews.items[0].updated_at >= after)
            )
        )
    ]


def format_plain(groups: dict[str, list[Item]]) -> str:
    def gen() -> Iterator[str]:
        first = True
        for repo, items in sorted(groups.items()):
            if not first:
                yield ""
            else:
                first = False
            yield f"**{repo}**"
            for item in items:
                label = "PR" if item.type == "PullRequest" else item.type
                yield f"- {label} #{item.number}: {item.title} <{item.url}>"

    return "\n".join(gen())


def format_html(groups: dict[str, list[Item]]) -> str:
    def gen() -> Iterator[str]:
        first = True
        yield '<div dir="ltr">'
        for repo, items in sorted(groups.items()):
            if not first:
                yield "<div><br></div>"
            else:
                first = False
            yield f"<div><b>{repo}</b></div>"
            for item in items:
                label = "PR" if item.type == "PullRequest" else item.type
                title = html.escape(item.title)
                count = item.comments.total_count + item.reviews.total_count
                suffix = f" <i>({count} comments)</i>" if count else ""
                yield (
                    f'<div>- <a href="{item.url}">{label} #{item.number}</a>'
                    f": {title}{suffix}</div>"
                )
        yield "</div>"

    return "".join(gen())


def send_email(
    address: str,
    username: str,
    password: str,
    subject: str,
    plain: str,
    html: str,
    smtp_host: str = "smtp.gmail.com",
    smtp_port: int = 465,
) -> None:
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = f"{username} <{address}>"
    msg["To"] = f"{username} <{address}>"

    plain_part = MIMEText(plain, "plain", "UTF-8")
    html_part = MIMEText(html, "html", "UTF-8")
    msg.attach(plain_part)
    msg.attach(html_part)

    context = ssl.create_default_context()
    with smtplib.SMTP_SSL(smtp_host, smtp_port, context=context) as server:
        server.login(address, password)
        server.send_message(msg)


def main() -> None:
    EMAIL_USERNAME = os.environ["EMAIL_USERNAME"]
    EMAIL_ADDRESS = os.environ["EMAIL_ADDRESS"]
    EMAIL_PASSWORD = os.environ["EMAIL_PASSWORD"]
    GH_TOKEN = os.environ["GH_TOKEN"]

    today = datetime.date.today()
    yesterday = today - datetime.timedelta(days=1)
    items = fetch_recent_items(GH_TOKEN, yesterday)
    items = select_interesting_items(items, yesterday)

    if items:
        groups = defaultdict(list)
        for item in items:
            groups[item.repo.name_with_owner].append(item)
        plain = format_plain(groups)
        html = format_html(groups)
        subject = f"GitHub Search Digest: msgspec ({today})"
        print(plain)
        send_email(EMAIL_ADDRESS, EMAIL_USERNAME, EMAIL_PASSWORD, subject, plain, html)


if __name__ == "__main__":
    main()
