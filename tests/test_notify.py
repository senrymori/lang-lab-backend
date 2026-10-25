"""Letters to learners: what signing in from a letter means, and a changed privacy policy (app/notify.py)."""
from datetime import date

from app import auth, mail, notify
from app.config import settings
from test_hosting import learner


def test_letters_that_make_an_account_say_it_means_accepting_the_policy():
    for subject, text, page in (auth.invite_letter("Анна", "LAB-1", "https://x/i"), auth._letter("register", "Анна", "123456", "https://x")):
        assert auth.CONSENT in text and settings.privacy_url in text and settings.privacy_url in page
    _, text, _ = auth._letter("login", "Анна", "123456", "https://x")  # signing in to an account made long ago
    assert auth.CONSENT not in text


async def test_a_changed_policy_is_a_letter_to_every_learner(capsys):
    await learner("Аня")
    await learner("Борис")
    mail.outbox.clear()
    assert await notify.policy("Добавили итальянский: его голоса озвучивает Azure.", date(2026, 11, 1), dry=True) == 0
    assert mail.outbox == [] and "Добавили итальянский" in capsys.readouterr().out
    sent = await notify.policy("Добавили итальянский: его голоса озвучивает Azure.", date(2026, 11, 1), dry=False)
    assert sent >= 2 and len(mail.outbox) == sent
    letter = mail.outbox[-1]
    assert "01.11.2026" in letter["text"] and "ничего делать не нужно" in letter["text"] and "удалим" in letter["text"]
