"""Configured loopback-only single-process uvicorn entry point for the administration backend."""

import uvicorn

from course_explorer.config import load_admin_web_settings
from course_explorer_admin import mechanics
from course_explorer_admin.remote_stages import resume_reboot_interrupted_stages
from course_explorer_admin_web.app import create_app


def main() -> None:
    """Serve the administration application on its configured loopback bind.

    One process is fixed, because it owns the in-memory sessions. The process starts one thread's
    event loop and one uvicorn worker, with access logging and proxy headers off. Before it
    serves, one best-effort resume relaunches the same-machine stage a host reboot interrupted. A
    failed resume prints the failure class and the console starts serving, because the console is
    the operator's only surface for the stage.
    """
    settings = load_admin_web_settings()
    try:
        resumed = resume_reboot_interrupted_stages(runner=mechanics.SubprocessRunner())
    except Exception as exc:
        print(f"a same-machine stage did not resume: {type(exc).__name__}", flush=True)
    else:
        if resumed:
            print(f"resumed after a host reboot: {', '.join(resumed)}", flush=True)
    uvicorn.run(
        create_app(settings=settings),
        host=settings.admin_web_host,
        port=settings.admin_web_port,
        workers=1,
        access_log=False,
        proxy_headers=False,
    )


if __name__ == "__main__":
    main()
