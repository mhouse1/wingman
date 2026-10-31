# Job Aid 013 — Join Mobius Squadron

| Status | Date       | Wingman Version |
|--------|------------|-----------------|
| Draft  | 2026-10-25 | 1.9.3           |

Wingman flies in MetalStorm as `[ISAF] 🄼🄾🄱🄸🅄🅂1`, a member of **Mobius
Squadron**. The squadron is open to human pilots as well. This job aid explains
where the name comes from and how to join.

## Where the name comes from

Mobius 1 is the player character of *Ace Combat 04: Shattered Skies*. He leads
Mobius Squadron, officially the 118th Tactical Fighter Wing of the Independent
State Allied Forces (ISAF) Air Force.

ISAF is the fictional military coalition of the series' 2003–2005 Continental
War, fought between the Usean Continental Nations and the Federal Republic of
Erusea. Its air force had several elite squadrons. The best known is Mobius
Squadron, which under Mobius 1 played a pivotal role in ISAF's counteroffensive
and in destroying the Erusean superweapon Megalith.

A fan theory, not canon, holds that Mobius 1 is an AI. Its supporters point to
his superhuman combat record, his lack of backstory, and the synthetic tone of
his ISAF briefing officers as signs that he is a machine built by ISAF. That
makes it a fitting call sign for an AI wingman.

## How to join

1. In the MetalStorm lobby, open **SQUADRON**.
2. Find **Mobius Squadron**, tag `[ISAF]`, and ask to join.

## Call signs (optional)

Members can take a numbered call sign in the squadron's style:

- `[ISAF] 🄼🄾🄱🄸🅄🅂0`
- `[ISAF] 🄼🄾🄱🄸🅄🅂1`
- `[ISAF] 🄼🄾🄱🄸🅄🅂2`
- `[ISAF] 🄼🄾🄱🄸🅄🅂3`

## Set a call sign in the game

MetalStorm allows one name change per account, so paste the call sign rather
than typing it. The keyboard cannot type the boxed letters anyway.

Copying on the desktop does not work: Ctrl-V in the game pastes nothing. The
game runs on its own X display, `:3` ([ADR 099](../adr/099-nested-display-lane-for-unattended-operation.md)), which has its own
clipboard. Put the call sign on that clipboard instead:

1. Launch the account (`make r3`, for example) and wait for the lobby.
2. Press **Backspace** in the game to put Wingman on standby, so it does not
   click lobby buttons while the name dialog is open.
3. In a terminal:

   ```bash
   make clip TEXT='🄼🄾🄱🄸🅄🅂3'
   ```

   It prints `clipboard on :3 holds '🄼🄾🄱🄸🅄🅂3' for 900 s` and keeps running.
4. Open the name change in the game, click the name field and press
   **Ctrl-V**. The terminal prints `pasted`.
5. Check the name in the field before you confirm. You get one change.

The clipboard stays set for 15 minutes (`SECONDS=60` for less), until anything
else is copied on `:3`, or until the display closes. Ctrl-C ends it early.
`make clip` prints `start the game first` when `:3` is not running.

Used on 2026-10-25 to name acct3 `🄼🄾🄱🄸🅄🅂3`.
