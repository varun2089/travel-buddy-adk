import argparse
import json

from travel_agent.db.policy_repository import (
    activate_policy, create_policy_session_factory, deactivate_policy, list_policies,
)


def main():
    parser = argparse.ArgumentParser(description="Manage explicitly activated synthetic policies.")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("list")
    activate = commands.add_parser("activate")
    activate.add_argument("document_id")
    activate.add_argument("version")
    deactivate = commands.add_parser("deactivate")
    deactivate.add_argument("document_id")
    args = parser.parse_args()
    factory = None
    try:
        factory = create_policy_session_factory()
        if args.command == "list":
            result = {"status": "ok", "policies": list_policies(factory)}
        elif args.command == "activate":
            result = activate_policy(factory, args.document_id, args.version)
        else:
            result = deactivate_policy(factory, args.document_id)
        print(json.dumps(result, indent=2))
        return 0
    except ValueError:
        print(json.dumps({"status": "error", "message": "Invalid policy selection or configuration; select an ingested version."}))
        return 2
    except Exception:
        print(json.dumps({"status": "error", "message": "Policy administration failed; check database configuration and migrations."}))
        return 1
    finally:
        if factory is not None:
            factory.kw["bind"].dispose()


if __name__ == "__main__":
    raise SystemExit(main())
