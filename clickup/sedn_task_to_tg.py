"""
Webhook handlers for accountant notifications when payment status changes.
"""

from typing import List, Dict, Any, Optional
from clickup_sdk.webhook import WebhookEvent

from core.dispatcher import dispatcher
from core.clickup_client import get_clickup_client
from core.logging_config import get_logger
from core.telegram_bot import send_message
from config.settings import get_settings
from utils.get_curstom_field_value import get_custom_field_value

logger = get_logger(__name__)

# Admin chat ID - can be moved to settings if needed
ADMIN_CHAT_ID = 1234567890


def format_assignees(assignees: List[Dict[str, Any]]) -> str:
    """
    Format assignees list into a readable string.

    Args:
        assignees: List of assignee dictionaries

    Returns:
        Formatted string with assignee names
    """
    if not assignees:
        return "Hech kim"

    names = []
    for assignee in assignees:
        if isinstance(assignee, dict):
            username = assignee.get("username", "")
            name = assignee.get("display_name") or assignee.get("name") or username
            if name:
                names.append(name)
        elif isinstance(assignee, str):
            names.append(assignee)

    return ", ".join(names) if names else "Noma'lum"


async def find_member_task_by_assignee_id(
    list_name: str,
    assignee_id: str,
) -> Optional[Dict[str, Any]]:
    """
    Find a task representing a member in a given list by ClickUp assignee ID.

    Strategy:
    - Query tasks for the team filtered by `assignees` (ClickUp user id).
    - Page through results and try to find a task where the task's list name
      matches `list_name` (case-insensitive).
    - As a fallback, check a few likely custom field names for a match.

    Returns:
        The matching task dict, or None if not found.
    """
    clickup_client = get_clickup_client()
    settings = get_settings()

    try:
        team_id = int(settings.TEAM_ID)
    except Exception:
        logger.error("❌ Invalid TEAM_ID in settings, cannot search tasks")
        return None

    page = 0
    max_pages = 10  # safety to avoid infinite paging

    while page < max_pages:
        try:
            resp = await clickup_client.tasks.get_tasks(
                team_id=team_id, assignees=[assignee_id], page=page
            )
        except Exception as exc:
            logger.error(
                f"❌ Failed to fetch tasks for assignee {assignee_id}: {exc}",
                exc_info=True,
            )
            return None

        # Normalize response to a task list
        tasks = None
        if isinstance(resp, dict):
            tasks = resp.get("tasks")
        elif isinstance(resp, list):
            tasks = resp

        if not tasks:
            break

        for task in tasks:
            # Match by list name first (preferred approach)
            list_info = task.get("list", {}) or {}
            list_name_from_task = (list_info.get("name") or "").strip()

            if list_name_from_task and list_name_from_task.lower() == list_name.lower():
                logger.debug(
                    f"Found member task {task.get('id')} in list {list_name_from_task} for assignee {assignee_id}"
                )
                return task

            if list_name.lower() in list_name_from_task.lower():
                return task

            # Fallback: check common custom field names that might store the assignee id
            for candidate in ("assignee_id", "user_id", "member_id", "clickup_id"):
                cf_val = get_custom_field_value(task, candidate)
                if cf_val is None:
                    continue
                # Extract simple string/int representations
                try:
                    if isinstance(cf_val, list) and cf_val:
                        cf_str = str(cf_val[0])
                    elif isinstance(cf_val, dict):
                        cf_str = str(cf_val.get("id") or list(cf_val.values())[0])
                    else:
                        cf_str = str(cf_val)
                except Exception:
                    cf_str = str(cf_val)

                if str(assignee_id) == cf_str:
                    logger.debug(
                        f"Found member task {task.get('id')} by custom field {candidate} for assignee {assignee_id}"
                    )
                    return task

        page += 1

    logger.info(
        f"⚠️ No member task found for assignee {assignee_id} in list {list_name}"
    )
    return None


@dispatcher.on("taskAssigneeUpdated")
async def notify_admin_on_assignee_change(event: WebhookEvent) -> None:
    """
    Notify admin when task assignee changes.
    """
    logger.info(f"👤 Assignee change detected. Task ID: {event.task_id}")

    clickup_client = get_clickup_client()

    try:
        task = await clickup_client.tasks.get_task(event.task_id)
    except Exception as exc:
        logger.error(f"❌ Failed to fetch task {event.task_id}: {exc}", exc_info=True)
        return

    # Get task information
    task_name = task.get("name", "N/A")
    task_url = task.get("url", "")
    task_assignees = task.get("assignees", [])

    # Debug: log task assignees directly
    logger.info(f"📋 Task assignees from API: {task_assignees}")

    # Get list information
    list_info = task.get("list", {})
    list_name = list_info.get("name", "N/A")

    # Always use current task assignees as the source of truth
    # Since taskAssigneeUpdated event is triggered, we know assignees have changed
    new_assignees = task_assignees if task_assignees else []
    new_assignees_str = format_assignees(new_assignees)

    # Try to extract old assignees from history_items for logging
    old_assignees = []
    old_assignees_str = "Hech kim"

    if event.history_items:
        logger.debug(f"📋 History items count: {len(event.history_items)}")
        for item in event.history_items:
            # Log the structure for debugging
            logger.debug(f"📋 History item structure: {item}")

            # Try different possible structures
            before = item.get("before")
            after = item.get("after")

            # Check if before/after are assignees directly
            if before:
                if isinstance(before, list):
                    old_assignees = before
                elif isinstance(before, dict):
                    old_assignees = before.get("assignees", [])
                    # Also try direct assignees key
                    if not old_assignees and "assignees" in str(before):
                        old_assignees = before.get("assignees", [])

            if after:
                if isinstance(after, list):
                    # This might be the new assignees, but we'll use task assignees instead
                    pass
                elif isinstance(after, dict):
                    # Check assignees in after dict
                    after_assignees = after.get("assignees", [])
                    if after_assignees:
                        # Compare with task assignees to see if there's a difference
                        pass

            # Also check item root level for assignees
            if "assignees" in item:
                item_assignees = item.get("assignees", [])
                if item_assignees:
                    old_assignees = item_assignees

            # Format for logging
            if old_assignees:
                old_assignees_str = format_assignees(old_assignees)
                logger.info(
                    f"  Assignees changed: {old_assignees_str} → {new_assignees_str}"
                )
                break

    # If we couldn't extract old assignees, just log current ones
    if old_assignees_str == "Hech kim":
        if new_assignees:
            logger.info(f"  Current task assignees: {new_assignees_str}")
        else:
            logger.warning(f"⚠️ No assignees found for task {event.task_id}")

    # If no assignees at all, exit early
    if not new_assignees:
        logger.warning(f"⚠️ Task has no assignees, skipping notification")
        return

    # Process each new assignee and send message to their Telegram
    for assignee in new_assignees:
        assignee_id = assignee.get("id")
        assignee_name = assignee.get("name") or assignee.get("username") or "Noma'lum"

        if not assignee_id:
            logger.warning(f"⚠️ Assignee ID not found for assignee: {assignee}")
            continue

        logger.info(
            f"🔍 Searching for member with assignee_id: {assignee_id} (Name: {assignee_name})"
        )

        # Find the member's task in "stuffs-extra-datas" list
        member_task = await find_member_task_by_assignee_id(
            list_name="stuffs-extra-datas", assignee_id=str(assignee_id)
        )

        if not member_task:
            logger.warning(f"⚠️ Member task not found for assignee_id: {assignee_id}")
            continue

        # Get telegram_id from the member task
        telegram_id = get_custom_field_value(member_task, "telegram_id")

        if not telegram_id:
            logger.warning(
                f"⚠️ Telegram ID not found for member with assignee_id: {assignee_id}"
            )
            continue

        # Convert telegram_id to int if it's a string number
        try:
            telegram_id_int = int(telegram_id)
        except (ValueError, TypeError):
            logger.error(
                f"❌ Invalid telegram_id format: {telegram_id} for assignee_id: {assignee_id}"
            )
            continue

        # Format message for the assignee
        message = (
            f"👤 <b>Sizga task berildi</b>\n\n"
            f"📋 <b>Task:</b> {task_name}\n"
            f"📂 <b>List:</b> {list_name}\n"
        )

        if task_url:
            message += f"\n🔗 <a href='{task_url}'>Taskni ko'rish</a>"

        # Send message to the assignee's Telegram
        success = send_message(telegram_id_int, message)
        if success:
            logger.info(
                f"✅ Message sent to {assignee_name} (Telegram ID: {telegram_id_int}) for task {event.task_id}"
            )
        else:
            logger.error(
                f"❌ Failed to send message to Telegram ID {telegram_id_int} for assignee_id {assignee_id}"
            )
