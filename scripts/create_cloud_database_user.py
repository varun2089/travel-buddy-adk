import getpass
import sys

import psycopg
from psycopg import sql


TABLES = (
    "policy_sections",
    "active_policies",
    "proposals",
    "reviews",
    "review_audit",
)


class SetupError(Exception):
    pass


def main():
    print("Inherited PUBLIC privileges still need review; this script does not change them.")
    try:
        administrator_password = getpass.getpass("Administrator password: ")
        application_password = getpass.getpass("New travel_app password: ")
        confirmation = getpass.getpass("Confirm new travel_app password: ")
        if not application_password:
            raise SetupError("The new password must not be empty.")
        if application_password != confirmation:
            raise SetupError("The new passwords do not match.")

        with psycopg.connect(
            host="127.0.0.1",
            port=5433,
            dbname="travel_buddy",
            user="postgres",
            password=administrator_password,
            connect_timeout=10,
            options="-c statement_timeout=15000 -c lock_timeout=5000",
        ) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = %s",
                    ("travel_app",),
                )
                if cursor.fetchone() is not None:
                    raise SetupError("travel_app already exists; no changes were made.")

                cursor.execute(
                    "SELECT c.relname FROM pg_catalog.pg_class AS c "
                    "JOIN pg_catalog.pg_namespace AS n ON n.oid = c.relnamespace "
                    "WHERE n.nspname = %s AND c.relname = ANY(%s) "
                    "AND c.relkind IN ('r', 'p')",
                    ("public", list(TABLES)),
                )
                present = {row[0] for row in cursor.fetchall()}
                missing = [name for name in TABLES if name not in present]
                if missing:
                    raise SetupError(
                        "Missing expected tables: "
                        + ", ".join("public." + name for name in missing)
                        + "; no changes were made."
                    )

                role = sql.Identifier("travel_app")
                cursor.execute(
                    sql.SQL(
                        "CREATE ROLE {} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE "
                        "NOREPLICATION NOBYPASSRLS PASSWORD {}"
                    ).format(role, sql.Literal(application_password))
                )
                cursor.execute(
                    sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(
                        sql.Identifier("travel_buddy"), role
                    )
                )
                cursor.execute(
                    sql.SQL("GRANT USAGE ON SCHEMA {} TO {}").format(
                        sql.Identifier("public"), role
                    )
                )
                cursor.execute(
                    sql.SQL("GRANT SELECT ON TABLE {} TO {}").format(
                        sql.SQL(", ").join(sql.Identifier("public", name) for name in TABLES),
                        role,
                    )
                )
                cursor.execute(
                    sql.SQL("GRANT INSERT ON TABLE {} TO {}").format(
                        sql.SQL(", ").join(
                            sql.Identifier("public", name)
                            for name in ("proposals", "reviews", "review_audit")
                        ),
                        role,
                    )
                )
                cursor.execute(
                    sql.SQL("GRANT UPDATE ({}, {}) ON TABLE {} TO {}").format(
                        sql.Identifier("status"),
                        sql.Identifier("confirmation"),
                        sql.Identifier("public", "reviews"),
                        role,
                    )
                )
    except SetupError as error:
        print(str(error), file=sys.stderr)
        return 1
    except (KeyboardInterrupt, EOFError):
        print("Cancelled; no changes were applied.", file=sys.stderr)
        return 1
    except Exception:
        print(
            "Database setup failed; the transaction was not completed. "
            "Verify the database state before retrying.",
            file=sys.stderr,
        )
        return 1

    print("Created travel_app and applied its permissions in one transaction.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
