import requests
from core.config import settings
from core.logger import get_logger

logger = get_logger(__name__)


class BrevoEmail:
    def __init__(self):
        self.base_url = "https://api.brevo.com/v3"

    def send_transactional_email(
        self, to_email: str, subject: str, message_content: str, api_key: str = None, sender_email: str = None, sender_name: str = None
    ):
        """
        Sends a single transactional email via Brevo API v3.
        Detects if content is already HTML; if not, converts newlines to <br/>.
        """
        final_api_key = api_key
        final_sender_email = sender_email
        final_sender_name = sender_name

        if not final_api_key:
            logger.warning("Brevo API Key missing. Mocking email success.")
            return {"status": "mock_success"}

        # 1. Format content: Only convert \n to <br/> if NOT already HTML
        formatted_content = message_content
        if not (
            "<p>" in message_content
            or "<br" in message_content
            or "</div>" in message_content
        ):
            formatted_content = message_content.replace("\n", "<br/>")

        html_body = f"""
        <html>
            <body style="font-family: 'Helvetica Neue', Helvetica, Arial, sans-serif; line-height: 1.6; color: #333; max-width: 600px; margin: 0 auto; padding: 20px; border: 1px solid #f0f0f0; border-radius: 12px;">
                <div style="font-size: 16px;">
                    {formatted_content}
                </div>
                <hr style="border: 0; border-bottom: 1px solid #eee; margin: 30px 0;" />
                <footer style="font-size: 12px; color: #999; text-align: center;">
                    &copy; {final_sender_name} - Quality and Affordable Always
                </footer>
            </body>
        </html>
        """

        url = f"{self.base_url}/smtp/email"
        payload = {
            "sender": {"name": final_sender_name, "email": final_sender_email},
            "to": [{"email": to_email}],
            "subject": subject,
            "htmlContent": html_body,
        }
        headers = {
            "accept": "application/json",
            "content-type": "application/json",
            "api-key": final_api_key,
        }

        try:
            response = requests.post(url, json=payload, headers=headers, timeout=10)
            if response.status_code in [200, 201, 202]:
                return {
                    "status": "success",
                    "message_id": response.json().get("messageId"),
                }
            else:
                return {"status": "failed", "error": response.text}
        except Exception as e:
            logger.error(f"Unexpected Brevo Email Error: {str(e)}", exc_info=True)
            return {"status": "error", "error": str(e)}


# Singleton instance
email_service = BrevoEmail()
