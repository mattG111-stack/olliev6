"""Conservative aliases: preserve unknown locations instead of guessing."""
ALIASES={"auckland cent":"Auckland Central","auckland cental":"Auckland Central",
 "auckland centra":"Auckland Central","auckland central":"Auckland Central",
 "st heliers":"Saint Heliers","saint heliers":"Saint Heliers",
 "st johns":"Saint Johns","saint johns":"Saint Johns",
 "st marys bay":"Saint Marys Bay","saint marys bay":"Saint Marys Bay",
 "pt chevalier":"Point Chevalier","point chevalier":"Point Chevalier",
 "wai o taiki bay":"Waiotaiki Bay","waiotaiki bay":"Waiotaiki Bay"}
def canonical_suburb(value):
    if not isinstance(value,str):return value
    text=" ".join(value.strip().strip(",").split())
    return ALIASES.get(text.casefold(),text)
def suburb_aliases(value):
    name=canonical_suburb(value).casefold()
    return sorted({name,*[key for key,label in ALIASES.items() if label.casefold()==name]})
