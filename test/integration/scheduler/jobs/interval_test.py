import pytest
from brewtils.models import IntervalTrigger, RequestTemplate, Job
import time

try:
    from helper import wait_for_response
    from helper.assertion import assert_successful_request, assert_validation_error
except:
    from ...helper import wait_for_response
    from ...helper.assertion import assert_successful_request, assert_validation_error


# @pytest.fixture(scope="class")
@pytest.fixture()
def system_spec():
    return {'system': 'echo', 'system_version': '3.0.0.dev0', 'instance_name': 'default',
            'command': 'say'}


@pytest.mark.usefixtures('easy_client')
class TestInterval(object):

    def test_no_namespace_job(self):

        job_name = "test_no_namespace_job"
        template = RequestTemplate(
            system='echo',
            system_version='3.0.0.dev0',
            instance_name='default',
            command='say',
            parameters={
                'message': "hello",
                'loud': False
            },
            comment=job_name + ' Job'
        )

        trigger = IntervalTrigger(minutes=1)
        trigger.reschedule_on_finish = True

        job = Job(
            name=job_name,
            trigger_type='interval',
            trigger=trigger,
            request_template=template,
            status="RUNNING",
            coalesce=True,
            max_instances=1
        )

        job_response = self.easy_client.create_job(job)

        assert job_response is not None

        # Wait a minute before checking plus a little extra
        time.sleep(60 + 15)

        job_update = self.easy_client.get_job(job_response.id)

        print(job_update)

        requests = self.easy_client.find_requests()

        request_found = False

        for request in requests:
            print(request.comment)
            if request.comment == job_name + ' Job':
                request_found = True
                break

        assert request_found


