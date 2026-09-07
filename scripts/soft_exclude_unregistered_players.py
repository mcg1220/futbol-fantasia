"""
One-off: soft-exclude players who aren't registered on any Premier League
roster this season from being pickable in Add/Drop, the Main Draft pool,
or a Transfer Draft pool.

Found via scripts/cross_reference_fantrax_clubs.py's "no Fantrax match"
bucket (players on a current-season club with no match, exact or fuzzy,
in a Fantrax export snapshot), then narrowed down further by hand:

  - 4 real players confirmed by the commissioner to have transferred out
    of the Premier League entirely (to clubs in other countries) --
    Rodri, Cristian Romero, Guglielmo Vicario, Tijjani Reijnders --
    correctly excluded.
  - 5 players removed from this list because they turned out to be
    genuine Fantrax matches under a mononym/shortened name that the
    normal exact/fuzzy name matching missed (e.g. "Allan Elias" is
    listed on Fantrax simply as "Allan") -- these are NOT included
    below; they remain fully draftable.
  - The other ~108 look like fringe/reserve/academy/inactive players
    Fantrax's pool doesn't track, consistent with "not registered on any
    Premier League roster this year" -- these are the ones this script
    soft-excludes.

Mirrors the existing soft-exclude convention (see
scripts/apply_fantrax_reimport.py's "removed" bucket): sets
players.draftable = 0 and drops any stale 2026-27 player_projections row
-- the player row itself, and all historical stats/eligibility, stay
completely intact. This does NOT affect anyone already rostered by a
manager (draftable only gates the browse/pool listings, not an existing
roster spot) and does NOT touch any name spelling.

    python3 soft_exclude_unregistered_players.py            # dry run
    python3 soft_exclude_unregistered_players.py --apply
"""
import argparse
import sqlite3

from init_db import DB_PATH

SEASON = '2026-27'

NAMES = [
    'Marli Salmon',
    'Samuel Iling-Junior',
    'Ben Winterburn',
    'Enes Ünal',
    'Benjamin Arthur',
    'Luka Bentt',
    'Ollie Shield',
    'Yunus Konak',
    'Gaga Slonina',
    'Landon Emenalo',
    'Dean Benamar',
    'George King',
    'Joél Drakes-Thomas',
    'Joël Piroe',
    'Largie Ramazani',
    'Sebastiaan Bornauw',
    'Kieran Morrison',
    'Divine Mukasa',
    'Max Alleyne',
    'Rodri',
    'Tijjani Reijnders',
    'Diego León',
    'Park Seung-Soo',
    'Jenson Jones',
    'Leo Hjelde',
    'Tymur Tutierov',
    'Cristian Romero',
    'Djed Spence',
    'Guglielmo Vicario',
    'Rio Kyerematen',
    'Ashley Phillips',
    'Calum Scanlon',
    'Damola Ajayi',
    'Daniel Gore',
    'David Datro Fofana',
    'Ethan Wheatley',
    'George Hirst',
    'Harry Clarke',
    'Ismeal Kabia',
    'Issa Kaboré',
    'Jamie Donley',
    'Kaine Kesler-Hayden',
    'Kosta Nedeljkovic',
    'Luke Chambers',
    'Malachi Hardy',
    'Mark O\'Mahony',
    'Omari Kellyman',
    'Tayo Adaramola',
    'Yang Min-Hyeok',
    'Jack Whittaker',
    'Jaydon Jones',
    'Joshua Stephenson',
    'Jim Thwaites',
    'Enis Destan',
    'Ali Al Hamadi',
    'Mateo Joseph',
    'Juma Bah',
    'Finley Burns',
    'Jaden Heskey',
    'Luis Hemir Silva Semedo',
    'Rory Finneran',
    'Harry Byrne',
    'Harvey Broad',
    'Xavier Parker',
    'Joseph Gabriel',
    'Demiane Agustien',
    'Tye Hall',
    'Josh Ogunnaike',
    'Ife Ibrahim',
    'Ted Curd',
    'Dylan Charlton',
    'Ceadach O\'Neill',
    'Theo Julienne',
    'Mofe Jemide',
    'Mason Miley',
    'Trialist',
    'Matheos Ferreira',
    'Finn Geragusyan',
    'Reggie Walsh',
    'Ollie Pickles',
    'Luke Craggs',
    'Anthony Munda',
    'Tyler Tingey',
    'Miodrag Pivas',
    'Dastan Satpaev',
    'Freddie Lane',
    'Jaydan Kamason',
    'Talla Ndiaye',
    'Enzo Kana Biyik',
    'Charlie Dinsdale',
    'Dylan Thompson',
    'Felix Scott',
    'Calvin Diakite',
    'Ethan Williams',
    'Joshua Abe',
    'Trey Ogunsuyi',
    'Tom Proctor',
    'Tyrese Hall',
    'Ryan Kavuma-McQueen',
    'Joshua Sonni-Lambie',
    'Lucca Benetton',
    'Archie Lightfoot',
    'Matty Henderson-Hall',
    'Aidan Dausch',
    'Will Wright',
    'Charlie Walker-Smith',
    'Jack Wint',
    'Vakhtang Salia',
    'Ben Kindon',
    'Mason Melia',
    'Trevan Sanusi',
    'Jacob Devaney',]


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--apply', action='store_true', help='write changes (default: dry run)')
    args = ap.parse_args()

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    to_apply = []
    not_found = []
    already_done = []
    for name in NAMES:
        row = conn.execute("SELECT id, name, club, position, draftable FROM players WHERE name=?", (name,)).fetchone()
        if not row:
            not_found.append(name)
            continue
        if row['draftable'] == 0:
            already_done.append(name)
            continue
        to_apply.append(row)

    if not_found:
        print(f"{len(not_found)} name(s) not found in players -- skipping (check spelling):")
        for n in not_found:
            print(f"  {n}")
        print()

    if already_done:
        print(f"{len(already_done)} already draftable=0 -- skipping.\n")

    print(f"{len(to_apply)} player(s) to soft-exclude:")
    for row in to_apply:
        print(f"  #{row['id']} {row['name']} ({row['position']}, {row['club']})")

    if not to_apply:
        print("\nNothing to change.")
        conn.close()
        return

    if not args.apply:
        print(f"\nDry run. Re-run with --apply to write.")
        conn.close()
        return

    for row in to_apply:
        conn.execute("UPDATE players SET draftable=0 WHERE id=?", (row['id'],))
        conn.execute("DELETE FROM player_projections WHERE season=? AND player_name=?", (SEASON, row['name']))
    conn.commit()
    print(f"\nApplied. {len(to_apply)} player(s) soft-excluded.")
    conn.close()


if __name__ == '__main__':
    main()
