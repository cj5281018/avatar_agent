from getpass import getpass

import httpx


def main():
    with httpx.Client(base_url="http://127.0.0.1:8788", timeout=25, trust_env=False) as client:
        login = client.post("/v1/auth/login", json={
            "username": "admin", "password": getpass("Avatar admin password: "),
        })
        if not login.is_success:
            raise SystemExit(f"Avatar login failed (HTTP {login.status_code})")
        client.headers["Authorization"] = "Bearer " + login.json()["token"]
        try:
            paired = client.post("/v1/admin/hr/setup", json={"setupCode": getpass("HR setup code: ").strip()})
            if not paired.is_success:
                raise SystemExit(f"HR pairing failed (HTTP {paired.status_code})")
            print("Paired:", paired.json()["service"]["slug"])
        finally:
            client.post("/v1/auth/logout")


if __name__ == "__main__":
    main()

