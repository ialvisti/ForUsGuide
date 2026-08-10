"""Development-tree compatibility wrapper for the runtime replay CLI.

Production images intentionally omit ``scripts``. Use
``python -m api.replay_ticket_evaluation`` in operational runbooks.
"""

from api.replay_ticket_evaluation import (  # noqa: F401
    main,
    replay_dead_letter,
    resolve_authenticated_operator,
)


if __name__ == "__main__":
    raise SystemExit(main())
