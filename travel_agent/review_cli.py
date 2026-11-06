import argparse
import json

from travel_agent.db.session import create_session_factory
from travel_agent.services.proposals import review_proposal, show


def main():
    parser = argparse.ArgumentParser(description=(
        "Review synthetic travel proposals. NuTrip submission is simulated. "
        "Reviewer identity is unauthenticated at this stage."
    ))
    commands = parser.add_subparsers(dest="command", required=True)
    for command in ("show", "approve", "reject"):
        subparser = commands.add_parser(command)
        subparser.add_argument("proposal_id")
        if command != "show":
            subparser.add_argument("--reviewer", required=True)
            subparser.add_argument("--confirm", action="store_true", required=True)
    args = parser.parse_args()
    factory = None
    try:
        factory = create_session_factory()
        if args.command == "show":
            result = show(factory, args.proposal_id)
        else:
            result = review_proposal(factory, args.proposal_id, args.command,
                                     args.reviewer, args.confirm)
        print(json.dumps(result, indent=2))
    except ValueError as error:
        parser.exit(2, f"{error}\n")
    except Exception:
        parser.exit(1, "Database operation failed. Check configuration and migrations.\n")
    finally:
        if factory is not None:
            factory.kw["bind"].dispose()


if __name__ == "__main__":
    main()
