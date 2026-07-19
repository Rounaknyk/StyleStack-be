import unittest
from unittest.mock import MagicMock, patch

from app.services.account_deletion import (
    AccountDeletionError,
    _list_owned_storage_paths,
    delete_user_account,
)


class StoragePathTests(unittest.TestCase):
    def test_recursively_lists_only_files_below_user_prefix(self) -> None:
        bucket = MagicMock()

        def list_side_effect(prefix, _options):
            return {
                "firebase-1": [
                    {"name": "item.jpg", "id": "object-1", "metadata": {}},
                    {"name": "canvas-styles", "id": None, "metadata": None},
                ],
                "firebase-1/canvas-styles": [
                    {"name": "preview.png", "id": "object-2", "metadata": {}}
                ],
            }.get(prefix, [])

        bucket.list.side_effect = list_side_effect

        self.assertEqual(
            _list_owned_storage_paths(bucket, "firebase-1"),
            [
                "firebase-1/item.jpg",
                "firebase-1/canvas-styles/preview.png",
            ],
        )


class AccountDeletionTests(unittest.TestCase):
    def _client(self):
        client = MagicMock()
        client.storage.from_.return_value.list.return_value = [
            {"name": "item.jpg", "id": "object-1", "metadata": {}}
        ]
        client.table.return_value.select.return_value.eq.return_value.limit.return_value.execute.return_value.data = []
        return client

    @patch("app.services.account_deletion.get_firebase_app")
    @patch("app.services.account_deletion.auth.delete_user")
    def test_deletes_storage_then_database_then_firebase(
        self, delete_firebase_user, get_firebase_app
    ) -> None:
        client = self._client()
        events: list[str] = []
        bucket = client.storage.from_.return_value
        bucket.remove.side_effect = lambda _paths: events.append("storage")
        client.table.return_value.delete.return_value.eq.return_value.execute.side_effect = (
            lambda: events.append("database")
        )
        delete_firebase_user.side_effect = lambda *_args, **_kwargs: events.append(
            "firebase"
        )

        deleted = delete_user_account(client, "firebase-1")

        self.assertEqual(deleted, 1)
        self.assertEqual(events, ["storage", "database", "firebase"])
        bucket.remove.assert_called_once_with(["firebase-1/item.jpg"])
        delete_firebase_user.assert_called_once_with(
            "firebase-1", app=get_firebase_app.return_value
        )

    @patch("app.services.account_deletion.auth.delete_user")
    def test_storage_failure_stops_before_database_and_auth(
        self, delete_firebase_user
    ) -> None:
        client = self._client()
        client.storage.from_.return_value.remove.side_effect = RuntimeError("down")

        with self.assertRaises(AccountDeletionError) as raised:
            delete_user_account(client, "firebase-1")

        self.assertEqual(raised.exception.stage, "storage")
        client.table.return_value.delete.assert_not_called()
        delete_firebase_user.assert_not_called()


if __name__ == "__main__":
    unittest.main()
