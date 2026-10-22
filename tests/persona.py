"""The one user every test is about, and the people around her. All fictional.

The tests were first written about one real person: their name, their
school, their classmates, the firms they wrote to, the rooms their lectures
were in, and "he" for the user throughout. So the suite could not tell a
feature from that one life. A test of a draft passed because the draft said
that one name, a fixture of a stranger's mail was a real classmate's, and a
stray "he" in the code read as right. Worse, the test data published real
people's names and addresses.

Everything here is invented:

- Maya Lindqvist, she/her, so a "he" about the user stands out. She lives
  in Pittsburgh, on America/New_York, so the clock-pinned tests keep their
  hours, and is a product designer at Contoso.
- Her addresses are on reserved example domains. Where a provider's own
  domain matters to the code (Gmail's dots and `+tags`), the local part is
  `steve.test.user`, which is nobody's.
- Phone numbers are in the 555-0100 to 555-0199 block kept for fiction, on
  Pittsburgh's area code.

A test that needs her name, an address or her home writes it from here, or
as a literal copied from here. A new person in a test comes from the cast
below, or is plainly invented: never a real name.
"""

from __future__ import annotations

# --- Maya ---

NAME = "Maya Lindqvist"
FIRST = "Maya"
PRONOUNS = "she/her"
OCCUPATION = "product designer"
EMPLOYER = "Contoso"

# Her address: her Google account's, where a test connects one
# (tests/fakes/google.py), so mail to herself is not outward.
EMAIL = "maya@example.com"
# Her Gmail, and the `+agent` alias the agent's own mailbox lives at
# (agent/mailbox.py). Gmail's spelling rules are the point where these are
# used, so they are on gmail.com. Gmail ignores the dots, so the agent
# writes the alias dotless (`core.mailbox.canonical`).
GMAIL = "steve.test.user@gmail.com"
ALIAS = "steve.test.user+agent@gmail.com"
# Her work address, on Contoso's Exchange, and her own domain's mailbox.
WORK_EMAIL = "maya@contoso.example"
OWN_DOMAIN_EMAIL = "maya@lindqvist.example"
# Every address of hers, as the mailbox policy lists them.
ADDRESSES = (GMAIL, WORK_EMAIL, OWN_DOMAIN_EMAIL)

PHONE = "+14125550100"

HOME_CITY = "Pittsburgh, PA"
ZONE = "America/New_York"
# Her home as the geocoder gives it: name, latitude, longitude.
HOME = ("Pittsburgh, Pennsylvania", 40.44062, -79.99589)

# --- The people around her ---

PARTNER = "Theo Marsh"
FRIENDS = ("Dana Ortiz", "Sam Ortiz", "Alex Kim")
# Colleagues, contacts and senders the tests' mail and calendars hold.
CAST = (
    "Owen Tate", "Iris Novak", "Alexandra Wren", "Alex Arden", "Mira Shah",
    "Hugo Lind", "Rina Sato", "Lena Fischer", "Nora Quinn", "Tomas Varga",
    "Ada Brook", "Marcus Bell", "Zoe Park", "Kai Morrow", "Omar Haddad",
    "Jordan Pell", "Kim Hale", "Leo Grant", "Ruth Kane", "Ray Ellis",
    "Clara Moss", "Clara Lee", "Ben Moss", "Nina", "Petra Lowe", "Hazel",
    "Arjun Mehta", "June Harper", "Tobi Okafor", "Asha Verma",
)

# --- Firms and places ---

FIRMS = {
    "Northwind": "northwind.example",
    "Fabrikam": "fabrikam.example",
    "Contoso": "contoso.example",
    "Tailspin": "tailspin.example",
}
# Rooms and places of her week.
PLACES = (
    "Larkspur Hall", "Aster Hall", "Foundry Hall", "Halden Museum",
    "Riverside Gym", "Ellery Field", "Juniper Cafe", "Harbor Hotel",
    "Birch Street", "Maple Street", "Ridgeview",
)
