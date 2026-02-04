# -*- coding: utf-8 -*-
from . import models


def _post_init_hook(env):
    """Archive duplicate project.task rows (same picking + driver) so DB is consistent."""
    Task = env["project.task"].sudo().with_context(active_test=False)
    if "logistics_picking_id" not in Task._fields or "logistics_driver_id" not in Task._fields:
        return
    tasks = Task.search(
        [
            ("logistics_picking_id", "!=", False),
            ("logistics_driver_id", "!=", False),
        ]
    )
    seen = {}
    to_archive = env["project.task"]
    for task in tasks:
        key = (task.logistics_picking_id.id, task.logistics_driver_id.id)
        if key in seen:
            to_archive |= task
        else:
            seen[key] = task
    if to_archive:
        to_archive.write({"active": False})
