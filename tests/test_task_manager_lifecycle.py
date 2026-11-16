import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import MagicMock, patch

from modules import task_manager as tm


class _Cursor:
    def __init__(self, row=None):
        self._row = row
        self.rowcount = 1

    def fetchone(self):
        return self._row


class _Connection:
    def __init__(self):
        self.update_calls = []

    def execute(self, sql, params=()):
        if sql.startswith('SELECT * FROM tasks'):
            return _Cursor({'id': 'task-1', 'status': tm.TASK_STATES['PENDING']})
        if sql.startswith('UPDATE tasks'):
            self.update_calls.append((sql, params))
            return _Cursor()
        raise AssertionError(sql)

    def commit(self):
        pass

    def rollback(self):
        pass

    def close(self):
        pass


class TaskLifecycleTests(unittest.TestCase):
    def tearDown(self):
        tm.clear_task_cancel('task-1', clear_flag=True)
        with tm._ACTIVE_TASKS_LOCK:
            tm._ACTIVE_TASK_IDS.discard('task-1')
        tm.shutdown_global_task_processor()

    def test_abandon_sets_cancel_and_blocks_successful_worker_write(self):
        connection = _Connection()
        with patch.object(tm, 'get_db_connection', return_value=connection):
            self.assertTrue(
                tm.update_task(
                    'task-1',
                    status=tm.TASK_STATES['FAILED'],
                    error_message='用户主动放弃任务',
                )
            )
            self.assertTrue(tm.is_task_cancelled('task-1'))
            self.assertFalse(
                tm.update_task(
                    'task-1',
                    status=tm.TASK_STATES['COMPLETED'],
                )
            )

        self.assertEqual(len(connection.update_calls), 1)

    def test_concurrent_lazy_initialization_creates_one_processor(self):
        instances = []
        barrier = threading.Barrier(8)

        class FakeProcessor:
            def __init__(self, config=None):
                time.sleep(0.01)
                instances.append(self)

            def refresh_config(self, config=None):
                pass

            def shutdown(self):
                pass

        def get_processor():
            barrier.wait(timeout=2)
            return tm.get_global_task_processor({})

        with patch.object(tm, 'TaskProcessor', FakeProcessor):
            with ThreadPoolExecutor(max_workers=8) as pool:
                futures = [pool.submit(get_processor) for _ in range(8)]
                results = [future.result(timeout=3) for future in futures]

        self.assertEqual(len(instances), 1)
        self.assertTrue(all(result is instances[0] for result in results))


if __name__ == '__main__':
    unittest.main()
