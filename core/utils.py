import html
import re


def clean_html(raw_html: str) -> str:
    """
    Removes HTML tags from a string and unescapes HTML entities.
    Useful for converting rich text (from editors like ReactQuill)
    into plain text for SMS delivery.
    """
    if not raw_html:
        return ""

    # 1. Replace common block/line-break tags with newlines to preserve basic structure
    content = (
        raw_html.replace("<br>", "\n").replace("<br/>", "\n").replace("<br />", "\n")
    )
    content = (
        content.replace("</p>", "\n").replace("</div>", "\n").replace("</li>", "\n")
    )

    # 2. Remove all remaining HTML tags using regex
    clean_re = re.compile("<.*?>")
    cleantext = re.sub(clean_re, "", content)

    # 3. Unescape HTML entities (e.g., &nbsp; -> space, &amp; -> &)
    cleantext = html.unescape(cleantext)

    # 4. Replace non-breaking spaces and multiple spaces with a single space
    cleantext = cleantext.replace("\xa0", " ")
    cleantext = re.sub(r" +", " ", cleantext)

    # 5. Strip leading/trailing whitespace
    return cleantext.strip()
