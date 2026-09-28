# Local monitoring (Prometheus + Grafana)

Shows how the Agro Nova API behaves: which model vendor answers, how slow each screen is, job outcomes, caches and
rate limits. Prometheus also evaluates [../alerts.yml](../alerts.yml). Both tools are open source and need no account.

## Start

1. Run the API on your machine (port 8000 assumed): `uvicorn app.main:app --port 8000` from `backend/`.
2. Give Prometheus the admin key (it is not stored in any committed file; `secrets/` is git-ignored):

   ```
   mkdir -p secrets
   printf '%s' "<ADMIN_API_KEY from backend/.env>" > secrets/admin_key
   ```

3. `docker compose up -d` in this folder.
4. Open Grafana at <http://localhost:3000> (login `admin` / `admin`); the "Agro Nova API" dashboard is the home page.
   Prometheus is at <http://localhost:9090> (Status > Targets should show `agro-nova-api` as UP; Alerts shows the rules).

Stop with `docker compose down` (add `-v` to also delete the stored history).

## Notes

- A different API port or a deployed instance: edit `targets` in [prometheus.yml](prometheus.yml).
- Change the dashboard: edit the `PANELS` list in [build_dashboard.py](build_dashboard.py) and run it.
- If a panel says "No data", the API has not served that kind of request yet (for example, no AI calls so far).
- The default Grafana password is only for local use. Change `GF_SECURITY_ADMIN_PASSWORD` before exposing it anywhere.
