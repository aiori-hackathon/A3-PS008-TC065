# AIORI-3 Hackathon: PS-008

**Network Management Agent for Reviewed Configuration Changes**
Category: Cybersecurity

## Team

| Role | Name |
|---|---|
| Team Lead | Mukul Goyal |
| Team Member | Ishika Pandey |
| Faculty Guide | Dr. Tejaswi Khanna |

## Team introduction video

https://youtu.be/WGWgj6Aavbs

## About the project

An agent that converts operator intents into router configuration, validates each change in a digital twin of a 3-router lab, applies it only after human approval, and rolls back automatically if deployment fails.

- **Lab:** 3 FRRouting routers in a triangle, run with Containerlab
- **Intents:** `ADD_ROUTE`, `REMOVE_ROUTE` (pending mentor confirmation)
- **Checks before approval:** schema, conflicting routes, routing loops, blackholes, syntax
- **Safety:** command allowlist, human approval gate, snapshot and all-router rollback, audit log

## Status

In development. Setup steps, test results and the report will be added here as they are completed.
