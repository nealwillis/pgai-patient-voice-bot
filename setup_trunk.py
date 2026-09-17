"""Create (or find) the LiveKit SIP outbound trunk that fronts our Twilio number.

Run once after filling in the Twilio values in .env:

    python setup_trunk.py

It prints the trunk ID; paste that into .env as LIVEKIT_SIP_TRUNK_ID. Running it
again is safe -- it reuses an existing trunk with the same name instead of
creating a duplicate.
"""

from __future__ import annotations

import asyncio

from livekit import api

from src.config import TARGET_NUMBER, env, require_env

TRUNK_NAME = "pgai-patient-bot"


async def main() -> None:
    termination_uri = require_env("TWILIO_TERMINATION_URI")
    caller_id = require_env("OUTBOUND_CALLER_ID")
    username = require_env("TWILIO_SIP_USERNAME")
    password = require_env("TWILIO_SIP_PASSWORD")

    # A termination URI pasted with the scheme still attached is a common slip
    # and produces a confusing SIP failure much later, so fix it here.
    address = termination_uri.removeprefix("sip:").removeprefix("sips:").strip("/")

    lkapi = api.LiveKitAPI(
        url=require_env("LIVEKIT_URL"),
        api_key=require_env("LIVEKIT_API_KEY"),
        api_secret=require_env("LIVEKIT_API_SECRET"),
    )

    try:
        existing = await lkapi.sip.list_outbound_trunk(
            api.ListSIPOutboundTrunkRequest()
        )
        for trunk in existing.items:
            if trunk.name == TRUNK_NAME:
                print(f"Reusing existing trunk {TRUNK_NAME!r}")
                _report(trunk, caller_id)
                return

        created = await lkapi.sip.create_outbound_trunk(
            api.CreateSIPOutboundTrunkRequest(
                trunk=api.SIPOutboundTrunkInfo(
                    name=TRUNK_NAME,
                    address=address,
                    numbers=[caller_id],
                    auth_username=username,
                    auth_password=password,
                    transport=api.SIP_TRANSPORT_AUTO,
                )
            )
        )
        print(f"Created trunk {TRUNK_NAME!r}")
        _report(created, caller_id)
    finally:
        await lkapi.aclose()


def _report(trunk, caller_id: str) -> None:
    print()
    print(f"  trunk id  : {trunk.sip_trunk_id}")
    print(f"  address   : {trunk.address}")
    print(f"  caller id : {', '.join(trunk.numbers) or '(none)'}")
    print()

    configured = env("LIVEKIT_SIP_TRUNK_ID")
    if configured == trunk.sip_trunk_id:
        print("  .env already points at this trunk. Nothing to do.")
    else:
        print("  Add this line to .env:")
        print(f"    LIVEKIT_SIP_TRUNK_ID={trunk.sip_trunk_id}")

    if caller_id not in trunk.numbers:
        print()
        print(f"  WARNING: OUTBOUND_CALLER_ID ({caller_id}) is not on this trunk.")
        print("  Twilio will reject the call. Update the trunk or fix .env.")

    print()
    print(f"  Calls will go to {TARGET_NUMBER} and nowhere else.")


if __name__ == "__main__":
    asyncio.run(main())
