"""A browser that leaves the upload page must not fail the running import.

Observed in production use (Windows, 18.2 M-line upload): after the user
navigated from the import page to the job list, the job failed ~28 s later with
``SourceChangedError`` (an uploaded source "became unavailable while it was
read").  Django closes ``request.FILES`` (and deletes the temporary upload) when
the request ends, but the import runs in a worker thread that was still reading
those files; and the worker blocked on the bounded progress queue because
nobody was consuming it any more.
"""

import time
from unittest import mock

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TransactionTestCase

from . import views
from .models import Account, ImportJob


def _wait_for_job(timeout=60):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        job = ImportJob.objects.order_by('-created_at').first()
        if job is not None and job.status not in ('pending', 'processing'):
            return job
        time.sleep(0.2)
    return ImportJob.objects.order_by('-created_at').first()


class ClientDisconnectTests(TransactionTestCase):
    LINES = 3000

    def _upload(self):
        body = '\n'.join(
            f'https://h{i % 40}.example/login|user{i}@x.example|pw{i}|N/A'
            for i in range(self.LINES)
        ).encode()
        return SimpleUploadedFile('big.txt', body, content_type='text/plain')

    def _disconnect_after_first_job_event(self, upload, closers_first=False):
        # Tiny batches and a tiny progress queue: many events, nobody reading.
        with mock.patch.object(views, 'STREAM_BATCH_SIZE', 250), \
                mock.patch.object(views, 'MAX_IMPORT_PROGRESS_EVENTS', 2):
            response = views._stream_import_progress([upload], [])
            stream = iter(response.streaming_content)
            for chunk in stream:
                if b'"type": "job"' in chunk:
                    break
            # What the WSGI server does when the socket is gone: close the
            # response (runs the generator's cleanup) and then Django closes
            # the request, which closes and deletes the uploaded files.
            response.close()
            upload.close()

    def test_import_completes_after_the_browser_disconnects(self):
        upload = self._upload()
        self._disconnect_after_first_job_event(upload)
        job = _wait_for_job()
        self.assertEqual(job.status, 'completed', job.last_error)
        self.assertEqual(job.total_committed, self.LINES)
        self.assertEqual(Account.objects.count(), self.LINES)

    def test_normal_streaming_still_reports_done(self):
        import json
        upload = self._upload()
        with mock.patch.object(views, 'STREAM_BATCH_SIZE', 250), \
                mock.patch.object(views, 'MAX_IMPORT_PROGRESS_EVENTS', 2):
            response = views._stream_import_progress([upload], [])
            events = [json.loads(c) for c in response.streaming_content]
            response.close()
            upload.close()
        self.assertEqual(events[-1]['type'], 'done')
        self.assertEqual(events[-1]['created'], self.LINES)
        self.assertEqual(Account.objects.count(), self.LINES)
