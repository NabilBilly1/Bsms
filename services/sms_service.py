import requests
from core.config import settings
from core.logger import get_logger

logger = get_logger(__name__)


class HubtelSMS:
    def __init__(self):
        # Hubtel Base URL (v1)
        self.base_url = "https://api.hubtel.com/v1/messages"

    def send_sms(self, to: str, message: str, client_id: str = None, secret: str = None, sender_id: str = None):
        """
        Sends an SMS using Hubtel's HTTP API.
        """
        final_client_id = client_id
        final_secret = secret
        final_sender_id = sender_id


        if not final_client_id or not final_secret:
            logger.warning("Hubtel credentials missing. Mocking success.")
            return {"status": "mock_success"}

        # Normalize phone number (ensure international format without +)
        clean_to = to.replace("+", "").strip()

        url = "https://smsc.hubtel.com/v1/messages/send"
        params = {
            "clientid": final_client_id,
            "clientsecret": final_secret,
            "from": final_sender_id,
            "to": clean_to,
            "content": message,
        }

        try:
            # We use GET as per the user's working URL example
            response = requests.get(url, params=params, timeout=15)

            # Hubtel SMSC GET API returns XML or plain text usually,
            # but let's check the response.
            if response.status_code in [200, 201]:
                return {"status": "success", "response": response.text}
            else:
                return {
                    "status": "failed",
                    "error": f"Hubtel Error: {response.text}",
                    "code": response.status_code,
                }

        except requests.exceptions.Timeout:
            logger.error("Hubtel SMS Timeout")
            return {
                "status": "error",
                "error": "Connection Timeout: Server might have no internet or Hubtel is down.",
            }
        except requests.exceptions.ConnectionError:
            logger.error("Hubtel SMS Connection Error")
            return {
                "status": "error",
                "error": "Connection Error: Failed to reach Hubtel servers. Check internet access.",
            }
        except Exception as e:
            logger.error(f"Unexpected Hubtel SMS Error: {str(e)}", exc_info=True)
            return {"status": "error", "error": f"Unexpected Error: {str(e)}"}


# Singleton instance
sms_service = HubtelSMS()
