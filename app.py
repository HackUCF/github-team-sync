import atexit
import json
import os
import sys
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import github3
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from flask import Flask

from githubapp import (
    ADD_MEMBER,
    CRON_INTERVAL,
    REMOVE_ORG_MEMBERS_WITHOUT_TEAM,
    SYNCMAP_ONLY,
    TEST_MODE,
    USER_SYNC_ATTRIBUTE,
    DirectoryClient,
    GitHubApp,
)
from githubapp.util import strtobool

app = Flask(__name__)
github_app = GitHubApp(app)

# Schedule a full sync
scheduler = BackgroundScheduler(daemon=True)
scheduler.start()
atexit.register(lambda: scheduler.shutdown(wait=False))


@github_app.on("team.created")
def sync_new_team() -> None:
    """
    Sync a new team when it is created
    :return:
    """
    owner = github_app.payload["organization"]["login"]
    team_id = github_app.payload["team"]["id"]
    if os.environ["USER_DIRECTORY"].upper() == "AAD":
        # Azure APIs don't currently support case insensitive searching
        slug = github_app.payload["team"]["name"].replace(" ", "-")
    else:
        slug = github_app.payload["team"]["slug"]
    client = github_app.installation_client
    sync_team(client=client, owner=owner, team_id=team_id, slug=slug)


def sync_team(
    client: Any = None,
    owner: str | None = None,
    team_id: int | None = None,
    slug: str | None = None,
) -> None:
    """
    Prepare the team sync
    :param client:
    :param owner:
    :param team_id:
    :param slug:
    :return:
    """
    print("-------------------------------")
    print(f"Processing Team: {slug}")

    try:
        org = client.organization(owner)
        team = org.team(team_id)
        custom_map, group_prefix, ignore_users = load_custom_map()
        try:
            directory_group = get_directory_from_slug(slug, custom_map, org)
            # If we're filtering on group prefix, skip if the group doesn't match
            if len(group_prefix) > 0 and not directory_group.startswith(
                tuple(group_prefix)
            ):
                print(f"skipping team {team.slug} - not in group prefix")
                return
            directory_members = directory_group_members(group=directory_group)
        except Exception:
            directory_members = []
            traceback.print_exc(file=sys.stderr)

        team_members = github_team_members(
            client=client,
            owner=owner,
            team_id=team_id,
            attribute=USER_SYNC_ATTRIBUTE,
            ignore_users=ignore_users,
        )
        compare = compare_members(
            group=directory_members, team=team_members, attribute=USER_SYNC_ATTRIBUTE
        )
        if TEST_MODE:
            print(f"TEST_MODE: Pending changes for team {team.slug}:")
            print(json.dumps(compare, indent=2))
        else:
            try:
                execute_sync(org=org, team=team, slug=slug, state=compare)
            except (AssertionError, ValueError) as e:
                if strtobool(os.environ["OPEN_ISSUE_ON_FAILURE"]):
                    open_issue(client=client, slug=slug, message=e)
                raise Exception(f"Team {team.slug} sync failed: {e}") from e
        print(f"Processing Team Successful: {team.slug}")
    except Exception:
        traceback.print_exc(file=sys.stderr)
        raise


def directory_group_members(group: str | None = None) -> list[dict[str, str | None]]:
    """
    Look up members of a group in your user directory
    :param group: The name of the group to query in your directory server
    :type group: str
    :return: group_members
    :rtype: list
    """
    try:
        directory = DirectoryClient()
        members = directory.get_group_members(group_name=group)
        group_members = [member for member in members]
    except Exception:
        group_members = []
        traceback.print_exc(file=sys.stderr)
    return group_members


def github_team_info(
    client: Any = None, owner: str | None = None, team_id: int | None = None
) -> Any:
    """
    Look up team info in GitHub
    :param client:
    :param owner:
    :param team_id:
    :return:
    """
    org = client.organization(owner)
    return org.team(team_id)


def github_team_members(
    client: Any = None,
    owner: str | None = None,
    team_id: int | None = None,
    attribute: str = "username",
    ignore_users: list[str] | None = None,
) -> list[dict[str, str]]:
    """
    Look up members of a given team in GitHub
    :param client:
    :param owner:
    :param team_id:
    :param attribute:
    :type owner: str
    :type team_id: int
    :type attribute: str
    :return: team_members
    :rtype: list
    """
    ignore_users = ignore_users or []
    team_members = []
    team = github_team_info(client=client, owner=owner, team_id=team_id)
    if attribute == "email":
        for m in team.members():
            user = client.user(m.login)
            team_members.append(
                {
                    "username": str(user.login),
                    "email": str(user.email),
                }
            )
    else:
        for member in team.members():
            team_members.append({"username": str(member), "email": ""})
    return [m for m in team_members if m["username"] not in ignore_users]


def compare_members(
    group: list[dict[str, str | None]],
    team: list[dict[str, str | None]],
    attribute: str = "username",
) -> dict[str, Any]:
    """
    Compare users in GitHub and the User Directory to see which users need to
    be added or removed
    :param group:
    :param team:
    :param attribute:
    :return: sync_state
    :rtype: dict
    """
    directory_list = [x[attribute].casefold() for x in group]
    github_list = [x[attribute].casefold() for x in team]
    add_users = list(set(directory_list) - set(github_list))
    remove_users = list(set(github_list) - set(directory_list))
    sync_state = {
        "directory": group,
        "github": team,
        "action": {"add": add_users, "remove": remove_users},
    }
    return sync_state


def execute_sync(org: Any, team: Any, slug: str, state: dict[str, Any]) -> None:
    """
    Perform the synchronization
    :param org:
    :param team:
    :param slug:
    :param state:
    :return:
    """
    total_changes = len(state["action"]["remove"]) + len(state["action"]["add"])
    threshold = int(os.environ.get("CHANGE_THRESHOLD", 25))
    if len(state["directory"]) == 0:
        directory = os.environ.get("USER_DIRECTORY", "LDAP").upper()
        message = f"{directory} group returned empty: {slug}"
        raise ValueError(message)
    elif int(total_changes) > threshold:
        message = (
            f"Skipping sync for {slug}.<br>"
            f"Total number of changes ({total_changes}) would exceed the "
            f"change threshold ({threshold})."
            "<br>Please investigate this change and increase your threshold "
            "if this is accurate."
        )
        raise AssertionError(message)
    else:
        for user in state["action"]["add"]:
            # Validate that user is in org
            if org.is_member(user) or ADD_MEMBER:
                try:
                    print(f"Adding {user} to {slug}")
                    team.add_or_update_membership(user)
                except github3.exceptions.NotFoundError:
                    print(f"User: {user} not found")
                    pass
            else:
                print(f"Skipping {user} as they are not part of the org")

        for user in state["action"]["remove"]:
            print(f"Removing {user} from {slug}")
            team.revoke_membership(user)


def open_issue(client: Any, slug: str, message: Exception | str) -> None:
    """
    Open an issue with the failed sync details
    :param client: Our installation client
    :param slug: Team slug
    :param message: Error message to detail
    :return:
    """
    repo_for_issues = os.environ["REPO_FOR_ISSUES"]
    owner = repo_for_issues.split("/")[0]
    repository = repo_for_issues.split("/")[1]
    assignee = os.environ["ISSUE_ASSIGNEE"]
    client.create_issue(
        owner=owner,
        repository=repository,
        assignee=assignee,
        title=f"Team sync failed for @{owner}/{slug}",
        body=str(message),
    )


def load_custom_map(
    file: str = "syncmap.yml",
) -> tuple[dict, list[str], list[str]]:
    """
    Custom team synchronization
    :param file:
    :return:
    """
    syncmap = {}
    ignore_users = []
    group_prefix = []
    if os.path.isfile(file):
        from yaml import Loader, load

        with open(file) as f:
            data = load(f, Loader=Loader)
        if "mapping" in data:
            for d in data["mapping"]:
                if "org" in d:
                    syncmap[(d["org"], d["github"])] = d["directory"]
                else:
                    syncmap[d["github"]] = d["directory"]
        else:
            syncmap = {}
        group_prefix = data.get("group_prefix", [])
        ignore_users = data.get("ignore_users", [])

    return (syncmap, group_prefix, ignore_users)


def get_app_installations() -> Any:
    """
    Get a list of installations for this app
    :return:
    """
    with app.app_context() as ctx:
        try:
            c = ctx.push()
            gh = GitHubApp(c)
            installations = gh.app_client.app_installations
        finally:
            ctx.pop()
    return installations


@scheduler.scheduled_job(
    trigger=CronTrigger.from_crontab(CRON_INTERVAL), id="sync_all_teams"
)
def sync_all_teams() -> None:
    """
    Lookup teams in a GitHub org and synchronize all teams with your user directory
    :return:
    """

    print(f"Syncing all teams: {time.strftime('%A, %d. %B %Y %I:%M:%S %p')}")

    installations = get_app_installations()
    custom_map, _, _ = load_custom_map()
    futures = []
    install_count = 0
    with ThreadPoolExecutor(max_workers=10) as exe:
        for i in installations():
            install_count += 1
            print("========================================================")
            print(f"## Processing Organization: {i.account['login']}")
            print("========================================================")
            with app.app_context() as ctx:
                try:
                    gh = GitHubApp(ctx.push())
                    client = gh.app_installation(installation_id=i.id)
                    org = client.organization(i.account["login"])
                    for team in org.teams():
                        futures.append(
                            exe.submit(sync_team_helper, team, custom_map, client, org)
                        )
                except Exception as e:
                    print(f"DEBUG: {e}")
                finally:
                    ctx.pop()
    if not install_count:
        raise Exception(f"No installation defined for APP_ID {os.getenv('APP_ID')}")
    for future in futures:
        future.result()
    if REMOVE_ORG_MEMBERS_WITHOUT_TEAM:
        remove_org_members_without_team(installations)
    print(f"Syncing all teams successful: {time.strftime('%A, %d. %B %Y %I:%M:%S %p')}")


def remove_org_members_without_team(installations: Any) -> None:
    for i in installations():
        with app.app_context() as ctx:
            try:
                gh = GitHubApp(ctx.push())
                client = gh.app_installation(installation_id=i.id)
                org = client.organization(i.account["login"])
                org_members = [member for member in org.members()]
                team_members = [
                    member for team in org.teams() for member in team.members()
                ]
                remove_members = list(set(org_members) - set(team_members))
                for member in remove_members:
                    print(f"Removing {member}")
                    if not TEST_MODE:
                        org.remove_membership(str(member))
            except Exception as e:
                print(f"DEBUG: {e}")
            finally:
                ctx.pop()


def sync_team_helper(team: Any, custom_map: dict, client: Any, org: Any) -> None:
    print(f"Organization: {org.login}")
    try:
        if SYNCMAP_ONLY and not is_team_in_map(team.slug, custom_map, org):
            print(f"skipping team {team.slug} - not in sync map")
            return
        sync_team(
            client=client,
            owner=org.login,
            team_id=team.id,
            slug=team.slug,
        )
    except Exception as e:
        print(f"Organization: {org.login}")
        print(f"Unable to sync team: {team.slug}")
        print(f"DEBUG: {e}")


def is_team_in_map(slug: str, custom_map: dict, org: Any) -> bool:
    key_with_org = (org.login, slug)
    key_without_org = slug
    if key_with_org in custom_map or key_without_org in custom_map:
        return True
    else:
        return False


def get_directory_from_slug(slug: str, custom_map: dict, org: Any) -> str | None:
    if not is_team_in_map(slug, custom_map, org):
        return slug
    elif (org.login, slug) in custom_map:
        return custom_map[(org.login, slug)]
    elif slug in custom_map:
        return custom_map[slug]


if "FLASK_APP" in os.environ:
    thread = threading.Thread(target=sync_all_teams)
    thread.start()

if __name__ == "__main__":
    if "FLASK_APP" in os.environ:
        app.run(
            host=os.environ.get("FLASK_RUN_HOST", "0.0.0.0"),
            port=os.environ.get("FLASK_RUN_PORT", "5000"),
        )
    else:
        sync_all_teams()
