CREATE ROLE travel_session_owner NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;
CREATE ROLE travel_session_runtime LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;
CREATE SCHEMA adk_sessions AUTHORIZATION travel_session_owner;
REVOKE ALL ON SCHEMA adk_sessions FROM PUBLIC;
GRANT CONNECT ON DATABASE travel_buddy TO travel_session_runtime;
GRANT USAGE ON SCHEMA adk_sessions TO travel_session_runtime;
GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE
    adk_sessions.sessions, adk_sessions.events, adk_sessions.app_states, adk_sessions.user_states
    TO travel_session_runtime;
GRANT SELECT ON TABLE adk_sessions.adk_internal_metadata TO travel_session_runtime;
