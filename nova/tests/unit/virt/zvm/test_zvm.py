# Copyright 2013 IBM Corp.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.


"""Test suite for ZVMDriver."""


from nova import context
from nova import test


class ZVMTestCase(test.TestCase):
    """Base testcase class of zvm driver and zvm instance."""

    def setUp(self):
        super(ZVMTestCase, self).setUp()
        self.context = context.get_admin_context()
        self.flags(host='fakehost',
                   my_ip='10.1.1.10')

    def test_init(self):
        pass
