from __future__ import annotations

import asyncio
from pathlib import Path

import click
from rich.console import Console
from rich.prompt import Prompt

console = Console()


@click.group()
def cli() -> None:
    """Open Executive — your AI-powered virtual executive team."""
    pass


@cli.command()
@click.argument("question")
def ask(question: str) -> None:
    """Ask the Executive a single question."""
    asyncio.run(_ask(question))


async def _ask(question: str) -> None:
    from openexecutive.knowledge.retriever import retrieve
    from openexecutive.memory.episodic import format_for_prompt
    from openexecutive.onboarding.profile_builder import load_or_create_profile
    from openexecutive.orchestrator.executive import Executive
    from openexecutive.orchestrator.session import Session

    profile = load_or_create_profile()
    if profile.is_empty():
        console.print(
            "[yellow]No company profile found. Run 'openexecutive onboard' to set one up.[/yellow]"
        )

    session = Session(
        company_profile=profile if not profile.is_empty() else None, from_cli=True
    )
    retrieved = retrieve(query=question)
    episodic = format_for_prompt()

    executive = Executive()

    console.print("\n[bold blue]Executive[/bold blue]\n")
    response = ""
    async for chunk in executive.stream_chat(
        user_message=question,
        session=session,
        retrieved_context=retrieved,
        episodic_context=episodic,
    ):
        if isinstance(chunk, str) and chunk != Executive._THINKING:
            response += chunk
            console.print(chunk, end="", highlight=False)

    console.print("\n")


@cli.command()
def chat() -> None:
    """Start an interactive chat session with the Executive."""
    asyncio.run(_chat())


async def _chat() -> None:
    from openexecutive.knowledge.retriever import retrieve
    from openexecutive.memory.episodic import format_for_prompt
    from openexecutive.onboarding.profile_builder import load_or_create_profile
    from openexecutive.orchestrator.executive import Executive
    from openexecutive.orchestrator.session import Session

    profile = load_or_create_profile()
    if profile.is_empty():
        console.print(
            "[yellow]No company profile found. Run 'openexecutive onboard' first.[/yellow]\n"
        )
    else:
        console.print(f"[green]Company profile loaded: {profile.name}[/green]\n")

    session = Session(
        company_profile=profile if not profile.is_empty() else None, from_cli=True
    )
    executive = Executive()

    console.print("[bold]Open Executive[/bold] — type 'exit' to quit\n")

    while True:
        try:
            user_input = Prompt.ask("[bold cyan]You[/bold cyan]")
        except (EOFError, KeyboardInterrupt):
            console.print("\n[dim]Goodbye.[/dim]")
            break

        if user_input.lower() in ("exit", "quit", "q"):
            console.print("[dim]Goodbye.[/dim]")
            break

        if not user_input.strip():
            continue

        retrieved = retrieve(query=user_input)
        episodic = format_for_prompt()

        console.print("\n[bold blue]Executive[/bold blue]\n")
        async for chunk in executive.stream_chat(
            user_message=user_input,
            session=session,
            retrieved_context=retrieved,
            episodic_context=episodic,
        ):
            if isinstance(chunk, str) and chunk != Executive._THINKING:
                console.print(chunk, end="", highlight=False)

        console.print("\n")


@cli.command("ingest-oer")
@click.option(
    "--phase",
    "phases",
    type=click.IntRange(1, 2),
    multiple=True,
    help="Restrict to phase 1 (CC BY / public domain) or 2 (CC BY-SA / MIT). Repeatable. Default: both.",
)
@click.option(
    "--source",
    "source_ids",
    multiple=True,
    help="Limit to specific source ids from sources.yaml. Repeatable.",
)
@click.option("--force", is_flag=True, help="Re-download cached files.")
def ingest_oer(phases: tuple[int, ...], source_ids: tuple[str, ...], force: bool) -> None:
    """Download and index open-licensed OER textbooks into the knowledge base."""
    asyncio.run(_ingest_oer(list(phases) or None, list(source_ids) or None, force))


async def _ingest_oer(
    phases: list[int] | None, source_ids: list[str] | None, force: bool
) -> None:
    from openexecutive.knowledge.external_sources import ingest_all

    results = await ingest_all(
        phases=phases, source_ids=source_ids, force=force, progress=console
    )

    total_chunks = sum(r.chunks for r in results)
    errors = [r for r in results if r.error]
    console.print(
        f"\n[bold]Done.[/bold] Indexed [green]{total_chunks}[/green] chunks "
        f"across {len(results) - len(errors)}/{len(results)} sources."
    )
    if errors:
        console.print("\n[red]Failures:[/red]")
        for r in errors:
            console.print(f"  - {r.source_id}: {r.error}")


@cli.command("sync-notion")
def sync_notion() -> None:
    """Run one Notion → isolated wiki-collection sync tick."""
    asyncio.run(_sync_notion())


async def _sync_notion() -> None:
    from openexecutive.config import get_settings
    from openexecutive.knowledge.notion_sync import run_notion_sync
    from openexecutive.knowledge.store import ChromaDBStore

    settings = get_settings()
    if not settings.notion_sync_enabled:
        console.print(
            "[yellow]NOTION_SYNC_ENABLED is false. Set it and NOTION_API_KEY in .env.[/yellow]"
        )
        return
    store = ChromaDBStore(persist_directory=settings.vector_store_path)
    stats = await run_notion_sync(store=store)
    console.print(f"[green]Notion sync:[/green] {stats}")


@cli.command("purge-notion")
@click.option("--page-id", default=None, help="Purge one synced page by Notion id.")
@click.option(
    "--stale",
    is_flag=True,
    help="Purge pages the integration no longer sees (requires NOTION_API_KEY).",
)
@click.option("--all", "purge_all", is_flag=True, help="Purge every locally synced page.")
def purge_notion(page_id: str | None, stale: bool, purge_all: bool) -> None:
    """Remove synced Notion files and Chroma chunks."""
    asyncio.run(_purge_notion(page_id, stale, purge_all))


async def _purge_notion(page_id: str | None, stale: bool, purge_all: bool) -> None:
    from openexecutive.config import get_settings
    from openexecutive.knowledge.notion_sync import (
        load_state,
        purge_all_synced,
        purge_page,
        run_notion_sync,
        save_state,
    )
    from openexecutive.knowledge.store import ChromaDBStore

    flags = sum(bool(x) for x in (page_id, stale, purge_all))
    if flags != 1:
        console.print(
            "[red]Specify exactly one of --page-id, --stale, or --all.[/red]"
        )
        return
    settings = get_settings()
    store = ChromaDBStore(persist_directory=settings.vector_store_path)
    if page_id:
        state = load_state()
        if purge_page(page_id, store, state):
            save_state(state)
            console.print(f"[green]Purged Notion page[/green] {page_id}")
        else:
            console.print(f"[red]Could not purge[/red] {page_id}")
        return
    if purge_all:
        n = purge_all_synced(store)
        console.print(f"[green]Purged {n} synced Notion page(s).[/green]")
        return
    if not settings.notion_sync_enabled:
        console.print(
            "[yellow]NOTION_SYNC_ENABLED is false. Set it and NOTION_API_KEY in .env.[/yellow]"
        )
        return
    stats = await run_notion_sync(store=store, reconcile_only=True)
    console.print(f"[green]Notion stale purge:[/green] {stats}")


@cli.command("sync-drive")
def sync_drive() -> None:
    """Run one Google Drive folder → isolated collection sync tick."""
    asyncio.run(_sync_drive())


async def _sync_drive() -> None:
    from openexecutive.config import get_settings
    from openexecutive.knowledge.drive_sync import run_drive_sync
    from openexecutive.knowledge.store import ChromaDBStore

    settings = get_settings()
    if not settings.drive_sync_enabled:
        console.print(
            "[yellow]DRIVE_SYNC_ENABLED is false. See docs/drive_sync_setup.md.[/yellow]"
        )
        return
    store = ChromaDBStore(persist_directory=settings.vector_store_path)
    stats = await run_drive_sync(store=store)
    console.print(f"[green]Drive sync:[/green] {stats}")


@cli.command("purge-drive")
@click.option("--file-id", default=None, help="Purge one synced file by Drive file id.")
@click.option(
    "--stale",
    is_flag=True,
    help="Purge files no longer in the synced folders (needs the sync configured).",
)
@click.option("--all", "purge_all", is_flag=True, help="Purge every locally synced file.")
def purge_drive(file_id: str | None, stale: bool, purge_all: bool) -> None:
    """Remove synced Google Drive files and Chroma chunks."""
    asyncio.run(_purge_drive(file_id, stale, purge_all))


async def _purge_drive(file_id: str | None, stale: bool, purge_all: bool) -> None:
    from openexecutive.config import get_settings
    from openexecutive.knowledge.drive_sync import (
        load_state,
        purge_all_synced,
        purge_file,
        run_drive_sync,
        save_state,
    )
    from openexecutive.knowledge.store import ChromaDBStore

    if sum(bool(x) for x in (file_id, stale, purge_all)) != 1:
        console.print("[red]Specify exactly one of --file-id, --stale, or --all.[/red]")
        return
    settings = get_settings()
    store = ChromaDBStore(persist_directory=settings.vector_store_path)
    if file_id:
        state = load_state()
        if purge_file(file_id, store, state):
            save_state(state)
            console.print(f"[green]Purged Drive file[/green] {file_id}")
        else:
            console.print(f"[red]Could not purge[/red] {file_id}")
        return
    if purge_all:
        n = purge_all_synced(store)
        console.print(f"[green]Purged {n} synced Drive file(s).[/green]")
        return
    if not settings.drive_sync_enabled:
        console.print(
            "[yellow]DRIVE_SYNC_ENABLED is false. See docs/drive_sync_setup.md.[/yellow]"
        )
        return
    stats = await run_drive_sync(store=store, reconcile_only=True)
    console.print(f"[green]Drive stale purge:[/green] {stats}")


@cli.command("sync-onedrive")
def sync_onedrive() -> None:
    """Run one OneDrive folder → isolated collection sync tick."""
    asyncio.run(_sync_onedrive())


async def _sync_onedrive() -> None:
    from openexecutive.config import get_settings
    from openexecutive.knowledge.onedrive_sync import load_state, run_onedrive_sync
    from openexecutive.knowledge.store import ChromaDBStore

    settings = get_settings()
    if not settings.onedrive_sync_enabled:
        console.print(
            "[yellow]ONEDRIVE_SYNC_ENABLED is false. See docs/onedrive_sync_setup.md.[/yellow]"
        )
        return
    store = ChromaDBStore(persist_directory=settings.vector_store_path)
    stats = await run_onedrive_sync(store=store)
    console.print(f"[green]OneDrive sync:[/green] {stats}")
    error = load_state().get("last_error")
    if isinstance(error, str) and error:
        console.print(f"[yellow]{error}[/yellow]")


@cli.command("purge-onedrive")
@click.option("--key", default=None, help="Purge one synced file by <drive id>:<item id>.")
@click.option(
    "--stale",
    is_flag=True,
    help="Purge files no longer in the synced folders (needs the sync configured).",
)
@click.option("--all", "purge_all", is_flag=True, help="Purge every locally synced file.")
def purge_onedrive(key: str | None, stale: bool, purge_all: bool) -> None:
    """Remove synced OneDrive files and Chroma chunks."""
    asyncio.run(_purge_onedrive(key, stale, purge_all))


async def _purge_onedrive(key: str | None, stale: bool, purge_all: bool) -> None:
    from openexecutive.config import get_settings
    from openexecutive.knowledge.onedrive_sync import (
        load_state,
        purge_all_synced,
        purge_file,
        run_onedrive_sync,
        save_state,
    )
    from openexecutive.knowledge.store import ChromaDBStore

    if sum(bool(x) for x in (key, stale, purge_all)) != 1:
        console.print("[red]Specify exactly one of --key, --stale, or --all.[/red]")
        return
    settings = get_settings()
    store = ChromaDBStore(persist_directory=settings.vector_store_path)
    if key:
        state = load_state()
        if purge_file(key, store, state):
            save_state(state)
            console.print(f"[green]Purged OneDrive file[/green] {key}")
        else:
            console.print(f"[red]Could not purge[/red] {key}")
        return
    if purge_all:
        n = purge_all_synced(store)
        console.print(f"[green]Purged {n} synced OneDrive file(s).[/green]")
        return
    if not settings.onedrive_sync_enabled:
        console.print(
            "[yellow]ONEDRIVE_SYNC_ENABLED is false. See docs/onedrive_sync_setup.md.[/yellow]"
        )
        return
    stats = await run_onedrive_sync(store=store, reconcile_only=True)
    console.print(f"[green]OneDrive stale purge:[/green] {stats}")


@cli.command("onedrive-folder")
@click.argument("link")
def onedrive_folder(link: str) -> None:
    """Turn a OneDrive or SharePoint folder sharing link into the
    <drive id>/<item id> entry ONEDRIVE_SYNC_FOLDERS takes. Reads as the
    Executive's Microsoft 365 sign-in, so the folder must be shared with it."""
    asyncio.run(_onedrive_folder(link))


async def _onedrive_folder(link: str) -> None:
    import httpx

    from openexecutive.config import get_settings
    from openexecutive.knowledge.onedrive_account import (
        OneDriveAuthTransient,
        OneDriveCredentialMissing,
        onedrive_token_provider,
    )
    from openexecutive.knowledge.onedrive_client import OneDriveClient, share_id

    try:
        share_id(link)
    except ValueError as exc:
        console.print(f"[red]{exc}.[/red] Paste the folder's https sharing link.")
        return
    try:
        token = onedrive_token_provider(get_settings())
        async with httpx.AsyncClient(timeout=30.0) as http:
            item = await OneDriveClient(http, token).resolve_share(link)
    except (OneDriveCredentialMissing, OneDriveAuthTransient) as exc:
        console.print(f"[red]Could not sign in to Microsoft 365:[/red] {exc}")
        return
    except httpx.HTTPStatusError as exc:
        console.print(
            f"[red]Microsoft answered {exc.response.status_code}.[/red] Check the link, "
            "and that the folder is shared with the Executive's Microsoft account."
        )
        return
    if item is None or not item.is_folder:
        console.print("[red]That link isn't to a folder.[/red] Share the folder itself.")
        return
    console.print(f"{item.name}: [green]{item.drive_id}/{item.id}[/green]")
    console.print("Add that to ONEDRIVE_SYNC_FOLDERS (comma-separated for several).")


@cli.command("directory-sync")
@click.option("--dry-run", is_flag=True, help="Print what it would change; change nothing.")
@click.option(
    "--allow-mass-suspend",
    is_flag=True,
    help="Run past the safety brake, after checking the realm and the group.",
)
def directory_sync_command(dry_run: bool, allow_mass_suspend: bool) -> None:
    """Run one directory sync: People follow the identity provider's group."""
    from openexecutive.integrations.directory_sync import sync

    report = sync(dry_run=dry_run, allow_mass_suspend=allow_mass_suspend)
    console.print(report.text(), markup=False)
    if report.stopped:
        raise SystemExit(1)


@cli.command("directory-link")
@click.option("--email", required=True, help="The address on both the Person and the directory member.")
@click.option(
    "--relink",
    is_flag=True,
    help="Re-point someone already managed whose identity was re-created at the provider.",
)
def directory_link_command(email: str, relink: bool) -> None:
    """Link an existing team Person to their directory identity."""
    from openexecutive.integrations.directory_sync import DirectoryError, link

    try:
        console.print(link(email, relink=relink), markup=False)
    except DirectoryError as error:
        console.print(f"Not linked: {error}", markup=False)
        raise SystemExit(1) from None


@cli.command("directory-demo")
@click.option("--email", required=True, help="The address of the Person the directory manages.")
@click.option("--off", is_flag=True, help="Make a demo person a team member instead.")
def directory_demo_command(email: str, off: bool) -> None:
    """Mark a directory-managed Person a demo person (or, with --off, not).
    The directory sync sets it only when it creates someone."""
    from openexecutive.integrations.directory_sync import DirectoryError, set_demo

    try:
        console.print(set_demo(email, not off), markup=False)
    except DirectoryError as error:
        console.print(f"Not changed: {error}", markup=False)
        raise SystemExit(1) from None


@cli.command("demo-reset")
@click.option(
    "--bundle",
    "bundle_dir",
    required=True,
    type=click.Path(file_okay=False, path_type=Path),
    help="The demo company bundle oe_connector wrote (bundle.json, profile.yaml, ...).",
)
def demo_reset_command(bundle_dir: Path) -> None:
    """Replace the demo company with a bundle, and delete demo people's own
    chats, cards and memory. Keeps directory-managed people, sign-ins,
    settings, the audit log and the principal's own state. Run it with the
    API stopped."""
    from openexecutive.cli.demo_reset import BundleError, demo_reset
    from openexecutive.config import get_settings

    try:
        summary = asyncio.run(demo_reset(bundle_dir, get_settings()))
    except BundleError as error:
        console.print(f"Not reset: {error}", markup=False)
        raise SystemExit(1) from None
    console.print(
        f"Reset {summary['company']} from the bundle made {summary['bundle_made_at']}"
        f" in {summary['seconds']}s: {summary['docs_indexed']} document chunks indexed,"
        f" {summary['people_seeded']} people added, {summary['departments_seeded']} departments,"
        f" {summary['demo_people']} demo people's own state deleted.",
        markup=False,
    )


@cli.command("principal")
@click.option("--email", required=True, help="The principal's own address, as they sign in.")
@click.option("--name", default=None, help="Their full name; needed when nobody has the address yet.")
@click.option("--role", default=None, help="Their role, e.g. Founder.")
def principal_command(email: str, name: str | None, role: str | None) -> None:
    """Name the principal, who runs this install, without onboarding: the
    team member with this address, or a new one. Changes nothing while
    someone else is the principal."""
    from openexecutive.people.principal import PrincipalError, set_principal

    try:
        console.print(set_principal(email, name=name, role=role), markup=False)
    except PrincipalError as error:
        console.print(f"Not set: {error}", markup=False)
        raise SystemExit(1) from None


@cli.command("draft-seals")
def draft_seals_command() -> None:
    """Print the seals OpenExecutive recorded for the ERPNext drafts it made,
    as JSON for oe_connector's `oe-connector-verify-drafts --expect`. Reads
    this install's decision ledger only. Exits 1 when it holds none, so an
    empty or wrong ledger is never a pass."""
    import json
    import sys

    from openexecutive.integrations.erpnext_drafts import recorded_seals

    seals = recorded_seals()
    sys.stdout.write(json.dumps(seals, indent=2, sort_keys=True) + "\n")
    click.echo(f"{len(seals)} recorded seals", err=True)
    if not seals:
        raise SystemExit(1)


@cli.command("directory-unlink")
@click.option("--email", required=True, help="The address of the Person the directory manages.")
def directory_unlink_command(email: str) -> None:
    """Stop the directory managing a Person; their row stays as it is."""
    from openexecutive.integrations.directory_sync import DirectoryError, unlink

    try:
        console.print(unlink(email), markup=False)
    except DirectoryError as error:
        console.print(f"Not unlinked: {error}", markup=False)
        raise SystemExit(1) from None


@cli.command("sync-confluence")
def sync_confluence() -> None:
    """Run one Confluence space → isolated collection sync tick."""
    asyncio.run(_sync_confluence())


async def _sync_confluence() -> None:
    from openexecutive.config import get_settings
    from openexecutive.knowledge.confluence_sync import run_confluence_sync
    from openexecutive.knowledge.store import ChromaDBStore

    settings = get_settings()
    if not settings.confluence_sync_enabled:
        console.print(
            "[yellow]CONFLUENCE_SYNC_ENABLED is false. "
            "See docs/confluence_sync_setup.md.[/yellow]"
        )
        return
    store = ChromaDBStore(persist_directory=settings.vector_store_path)
    stats = await run_confluence_sync(store=store)
    console.print(f"[green]Confluence sync:[/green] {stats}")


@cli.command("purge-confluence")
@click.option("--page-id", default=None, help="Purge one synced page by Confluence page id.")
@click.option(
    "--stale",
    is_flag=True,
    help="Purge pages no longer in the synced spaces, or now restricted (needs the sync configured).",
)
@click.option("--all", "purge_all", is_flag=True, help="Purge every locally synced page.")
def purge_confluence(page_id: str | None, stale: bool, purge_all: bool) -> None:
    """Remove synced Confluence pages and Chroma chunks."""
    asyncio.run(_purge_confluence(page_id, stale, purge_all))


async def _purge_confluence(page_id: str | None, stale: bool, purge_all: bool) -> None:
    from openexecutive.config import get_settings
    from openexecutive.knowledge.confluence_sync import (
        load_state,
        purge_all_synced,
        purge_page,
        run_confluence_sync,
        save_state,
    )
    from openexecutive.knowledge.store import ChromaDBStore

    if sum(bool(x) for x in (page_id, stale, purge_all)) != 1:
        console.print("[red]Specify exactly one of --page-id, --stale, or --all.[/red]")
        return
    settings = get_settings()
    store = ChromaDBStore(persist_directory=settings.vector_store_path)
    if page_id:
        state = load_state()
        if purge_page(page_id, store, state):
            save_state(state)
            console.print(f"[green]Purged Confluence page[/green] {page_id}")
        else:
            console.print(f"[red]Could not purge[/red] {page_id}")
        return
    if purge_all:
        n = purge_all_synced(store)
        console.print(f"[green]Purged {n} synced Confluence page(s).[/green]")
        return
    if not settings.confluence_sync_enabled:
        console.print(
            "[yellow]CONFLUENCE_SYNC_ENABLED is false. "
            "See docs/confluence_sync_setup.md.[/yellow]"
        )
        return
    stats = await run_confluence_sync(store=store, reconcile_only=True)
    console.print(f"[green]Confluence stale purge:[/green] {stats}")


@cli.command("consolidate-initiatives")
@click.option(
    "--apply",
    "apply_changes",
    is_flag=True,
    help="Apply the proposed merges. Default is dry-run.",
)
def consolidate_initiatives(apply_changes: bool) -> None:
    """Cluster duplicate active initiatives and (optionally) merge them.

    Run without --apply to preview the proposed consolidation. Re-run with
    --apply to commit the merges to the episodic database.

    NOTE: --apply takes an IMMEDIATE write lock on the SQLite database, so
    a concurrent chat turn that runs the background extraction pass will
    block briefly while the merge commits. For large consolidations,
    pause the API first.
    """
    asyncio.run(_consolidate_initiatives(apply_changes))


async def _consolidate_initiatives(apply_changes: bool) -> None:
    from openexecutive.memory.initiatives_consolidation import (
        apply_clusters,
        propose_clusters,
    )

    initiatives, clusters = await propose_clusters()
    if not clusters:
        console.print(
            f"[green]No duplicates detected.[/green] {len(initiatives)} active initiatives."
        )
        return

    title_by_id = {i.id: i.title for i in initiatives}
    total_members = sum(len(c.member_ids) for c in clusters)
    console.print(
        f"[bold]Proposed consolidation[/bold]: {total_members} rows → "
        f"{len(clusters)} canonical (net delete: {total_members - len(clusters)})\n"
    )
    for c in clusters:
        console.print(f"[cyan]→ {c.canonical_title}[/cyan]")
        for mid in c.member_ids:
            t = title_by_id.get(mid, f"<unknown id={mid}>")
            console.print(f"    id={mid}  {t}")
        console.print()

    if not apply_changes:
        console.print("[dim]Dry run. Re-run with --apply to merge.[/dim]")
        return

    result = apply_clusters(clusters)
    console.print(
        f"[green]Merged {result['clusters_merged']} clusters; "
        f"deleted {result['rows_deleted']} rows.[/green]"
    )


@cli.command(
    "make-caller-keys",
    context_settings={"ignore_unknown_options": True, "help_option_names": []},
    add_help_option=False,
)
@click.argument("args", nargs=-1, type=click.UNPROCESSED)
def make_caller_keys(args: tuple[str, ...]) -> None:
    """Make a key pair for signed callers (--help for the kinds).

    The same as scripts/make-caller-keys.py, for a server that runs the API
    image without a checkout: the private half is made where it is used.
    """
    from openexecutive.api.caller_keys import main

    raise SystemExit(main(list(args), prog="openexecutive make-caller-keys"))


@cli.command()
def onboard() -> None:
    """Set up or update your company profile."""
    asyncio.run(_onboard())


async def _onboard() -> None:
    from openexecutive.memory.workspace_settings import get_workspace
    from openexecutive.onboarding.profile_builder import build_and_save_profile
    from openexecutive.onboarding.wizard import (
        WizardState,
        get_current_question,
        process_answer,
    )

    console.print("[bold]Open Executive Onboarding[/bold]\n")
    console.print("This wizard will set up your company profile. Type 'skip' to skip optional questions.\n")

    # A solo workspace skips the team steps (see onboarding.wizard).
    state = WizardState(solo=get_workspace().mode == "solo")

    while not state.completed:
        question = get_current_question(state)
        if question is None:
            break

        step_num = state.position() + 1
        console.print(f"[dim]Step {step_num}/{state.total_steps()}[/dim]")
        console.print(f"[bold]{question}[/bold]\n")

        try:
            answer = Prompt.ask("> ")
        except (EOFError, KeyboardInterrupt):
            console.print("\n[yellow]Onboarding cancelled.[/yellow]")
            return

        state = process_answer(state, answer)
        console.print()

    profile = build_and_save_profile(state)
    console.print(
        f"\n[green]Company profile saved for '{profile.name}'.[/green]\n"
        "You can now start chatting: [bold]openexecutive chat[/bold]"
    )


if __name__ == "__main__":
    cli()
