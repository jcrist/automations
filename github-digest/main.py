from __future__ import annotations

import argparse
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
from urllib.parse import quote

import msgspec
import requests


ROOT = Path(__file__).absolute().parent.parent

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


class Repo(msgspec.Struct, rename="camel"):
    name_with_owner: str


class Actor(msgspec.Struct):
    type: str
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


class Commit(msgspec.Struct):
    class Repository(msgspec.Struct):
        full_name: str

    class CommitInfo(msgspec.Struct):
        class Author(msgspec.Struct):
            name: str

        class Committer(msgspec.Struct):
            date: datetime.datetime

        author: Author
        committer: Committer
        message: str

        @property
        def title(self) -> str:
            return self.message.splitlines()[0].strip()

    sha: str
    html_url: str
    node_id: str
    repo: Repository = msgspec.field(name="repository")
    info: CommitInfo = msgspec.field(name="commit")


class CommitSearchResults(msgspec.Struct):
    total_count: int
    items: list[Commit]


class CommitNodesResults(msgspec.Struct, rename="camel"):
    class Data(msgspec.Struct, rename="camel"):
        class CommitNode(msgspec.Struct, rename="camel"):
            class PullRequests(msgspec.Struct, rename="camel"):
                total_count: int

            associated_pull_requests: PullRequests

        nodes: list[CommitNode]

    data: Data


def fetch_recent_items(token: str, after: datetime.datetime) -> list[Item]:
    with requests.Session() as session:
        search = f"msgspec updated:>={after.date()}"
        items = []
        # Fetch recent issues, PRs, and discussions
        for file in ["issues.graphql", "discussions.graphql"]:
            with open(ROOT / "github-digest" / file, "r") as f:
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
                resp.raise_for_status()
                msg = msgspec.json.decode(resp.content, type=SearchResults).data.search
                items.extend(msg.items)
                if msg.page_info.has_next_page:
                    cursor = msg.page_info.end_cursor
                else:
                    break
    return [
        item
        for item in items
        if (
            item.author.login not in IGNORE_AUTHORS
            and item.author.type != "Bot"
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


def fetch_recent_commits(token: str, after: datetime.datetime) -> list[Commit]:
    commits = []

    with requests.Session() as session:
        # Fetch recent commits. This isn't exposed through graphql currently.
        query = quote(f"msgspec committer-date:>={after.date()}")
        resp = session.get(  # type: ignore
            (
                f"https://api.github.com/search/commits"
                f"?q={query}&sort=committer-date&order=desc&per_page=100"
            ),
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {token}",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )
        resp.raise_for_status()
        commit_search = msgspec.json.decode(resp.content, type=CommitSearchResults)

        if commit_search.items:
            node_ids = [commit.node_id for commit in commit_search.items]

            # Filter out commits that have an associated PR. This can only be
            # done by graphql.
            with open(ROOT / "github-digest" / "commits.graphql", "r") as f:
                template = f.read()
            query = template % msgspec.json.encode(node_ids).decode()
            resp = session.post(  # type: ignore
                "https://api.github.com/graphql",
                headers={"Authorization": f"bearer {token}"},
                json={"query": query},
            )
            resp.raise_for_status()
            nodes = msgspec.json.decode(
                resp.content, type=CommitNodesResults
            ).data.nodes
            for commit, node in zip(commit_search.items, nodes):
                if node.associated_pull_requests.total_count == 0:
                    commits.append(commit)

    return [
        commit
        for commit in commits
        if commit.info.committer.date >= after
        and commit.info.author.name not in IGNORE_AUTHORS
        and commit.repo.full_name not in IGNORE_REPOS
    ]


def format_plain(groups: dict[str, list[Item | Commit]]) -> str:
    def gen() -> Iterator[str]:
        first = True
        for repo, items in sorted(groups.items()):
            if not first:
                yield ""
            else:
                first = False
            yield f"**{repo}**"
            for item in items:
                if isinstance(item, Item):
                    label = "PR" if item.type == "PullRequest" else item.type
                    yield f"- {label} #{item.number}: {item.title} <{item.url}>"
                else:
                    yield (
                        f"- Commit {item.sha[:8]}: {item.info.title} <{item.html_url}>"
                    )

    return "\n".join(gen())


def format_html(groups: dict[str, list[Item | Commit]]) -> str:
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
                if isinstance(item, Item):
                    label = "PR" if item.type == "PullRequest" else item.type
                    title = html.escape(item.title)
                    count = item.comments.total_count + item.reviews.total_count
                    suffix = f" <i>({count} comments)</i>" if count else ""
                    yield (
                        f'<div>- <a href="{item.url}">{label} #{item.number}</a>'
                        f": {title}{suffix}</div>"
                    )
                else:
                    title = html.escape(item.info.title)
                    yield (
                        f'<div>- <a href="{item.html_url}">Commit {item.sha[:8]}</a>'
                        f": {title}</div>"
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
    parser = argparse.ArgumentParser("github-digest")
    parser.add_argument(
        "--no-email", action="store_true", help="Skip sending them email"
    )
    args = parser.parse_args()

    # Load .env file if present
    try:
        with open(ROOT / ".env", "r") as f:
            for line in f.readlines():
                key, val = line.strip().split("=", 1)
                val = val.removeprefix('"').removesuffix('"')
                os.environ[key] = val
    except FileNotFoundError:
        pass

    # Read secrets from environment
    EMAIL_USERNAME = os.environ["EMAIL_USERNAME"]
    EMAIL_ADDRESS = os.environ["EMAIL_ADDRESS"]
    EMAIL_PASSWORD = os.environ["EMAIL_PASSWORD"]
    GH_TOKEN = os.environ["GH_TOKEN"]

    today = datetime.date.today()
    yesterday = today - datetime.timedelta(days=1)
    # Find everything interesting after 9 central (14 UTC) yesterday
    after = datetime.datetime.combine(
        yesterday, datetime.time(14, tzinfo=datetime.timezone.utc)
    )

    items = fetch_recent_items(GH_TOKEN, after)
    commits = fetch_recent_commits(GH_TOKEN, after)

    if items or commits:
        groups: defaultdict[str, list[Item | Commit]] = defaultdict(list)
        for item in items:
            groups[item.repo.name_with_owner].append(item)
        for commit in commits:
            groups[commit.repo.full_name].append(commit)
        plain = format_plain(groups)
        html = format_html(groups)
        subject = f"GitHub Search Digest: msgspec ({today})"
        print(plain)
        if not args.no_email:
            send_email(
                EMAIL_ADDRESS,
                EMAIL_USERNAME,
                EMAIL_PASSWORD,
                subject,
                plain,
                html,
            )


if __name__ == "__main__":
    main()
