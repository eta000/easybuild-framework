##
# Copyright 2012-2018 Ghent University
#
# This file is part of EasyBuild,
# originally created by the HPC team of Ghent University (http://ugent.be/hpc/en),
# with support of Ghent University (http://ugent.be/hpc),
# the Flemish Supercomputer Centre (VSC) (https://www.vscentrum.be),
# Flemish Research Foundation (FWO) (http://www.fwo.be/en)
# and the Department of Economy, Science and Innovation (EWI) (http://www.ewi-vlaanderen.be/en).
#
# https://github.com/easybuilders/easybuild
#
# EasyBuild is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation v2.
#
# EasyBuild is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with EasyBuild.  If not, see <http://www.gnu.org/licenses/>.
##
"""
Unit tests for talking to GitHub.

@author: Jens Timmerman (Ghent University)
@author: Kenneth Hoste (Ghent University)
"""
import os
import random
import re
import string
import sys
from test.framework.utilities import EnhancedTestCase, TestLoaderFiltered, init_config
from unittest import TextTestRunner
from urllib2 import URLError

from easybuild.tools.build_log import EasyBuildError
from easybuild.tools.config import module_classes
from easybuild.tools.configobj import ConfigObj
from easybuild.tools.filetools import read_file, write_file
import easybuild.tools.github as gh

try:
    import keyring
    HAVE_KEYRING = True
except ImportError, err:
    HAVE_KEYRING = False


# test account, for which a token may be available
GITHUB_TEST_ACCOUNT = 'easybuild_testxxx'
# the user & repo to use in this test (https://github.com/hpcugent/testrepository)
GITHUB_USER = "hpcugent"
GITHUB_REPO = "testrepository"
# branch to test
GITHUB_BRANCH = 'master'


class GithubTest(EnhancedTestCase):
    """ small test for The github package
    This should not be to much, since there is an hourly limit of request
    for non authenticated users of 50"""

    def setUp(self):
        """setup"""
        super(GithubTest, self).setUp()
        self.github_token = gh.fetch_github_token(GITHUB_TEST_ACCOUNT)
        if self.github_token is None:
            self.ghfs = gh.Githubfs(GITHUB_USER, GITHUB_REPO, GITHUB_BRANCH, None, None, None)
        else:
            self.ghfs = gh.Githubfs(GITHUB_USER, GITHUB_REPO, GITHUB_BRANCH, GITHUB_TEST_ACCOUNT,
                                    None, self.github_token)

        self.skip_github_tests = self.github_token is None and os.getenv('FORCE_EB_GITHUB_TESTS') is None

    def test_walk(self):
        """test the gitubfs walk function"""
        if self.skip_github_tests:
            print "Skipping test_walk, no GitHub token available?"
            return

        try:
            expected = [(None, ['a_directory', 'second_dir'], ['README.md']),
                        ('a_directory', ['a_subdirectory'], ['a_file.txt']), ('a_directory/a_subdirectory', [],
                        ['a_file.txt']), ('second_dir', [], ['a_file.txt'])]
            self.assertEquals([x for x in self.ghfs.walk(None)], expected)
        except IOError:
            pass

    def test_read_api(self):
        """Test the githubfs read function"""
        if self.skip_github_tests:
            print "Skipping test_read_api, no GitHub token available?"
            return

        try:
            self.assertEquals(self.ghfs.read("a_directory/a_file.txt").strip(), "this is a line of text")
        except IOError:
            pass

    def test_read(self):
        """Test the githubfs read function without using the api"""
        if self.skip_github_tests:
            print "Skipping test_read, no GitHub token available?"
            return

        try:
            fp = self.ghfs.read("a_directory/a_file.txt", api=False)
            self.assertEquals(open(fp, 'r').read().strip(), "this is a line of text")
            os.remove(fp)
        except (IOError, OSError):
            pass

    def test_fetch_pr_data(self):
        """Test fetch_pr_data function."""
        if self.skip_github_tests:
            print "Skipping test_fetch_pr_data, no GitHub token available?"
            return

        pr_data, pr_url = gh.fetch_pr_data(1, GITHUB_USER, GITHUB_REPO, GITHUB_TEST_ACCOUNT)

        self.assertEquals(pr_data['number'], 1)
        self.assertEquals(pr_data['title'], "a pr")
        self.assertFalse(any(key in pr_data for key in ['issue_comments', 'review', 'status_last_commit']))

        pr_data, pr_url = gh.fetch_pr_data(2, GITHUB_USER, GITHUB_REPO, GITHUB_TEST_ACCOUNT, full=True)
        self.assertEquals(pr_data['number'], 2)
        self.assertEquals(pr_data['title'], "an open pr (do not close this please)")
        self.assertTrue(pr_data['issue_comments'])
        self.assertEquals(pr_data['issue_comments'][0]['body'], "this is a test")
        self.assertTrue(pr_data['reviews'])
        self.assertEquals(pr_data['reviews'][0]['state'], "APPROVED")
        self.assertEquals(pr_data['reviews'][0]['user']['login'], 'boegel')
        self.assertEqual(pr_data['status_last_commit'], 'pending')

    def test_list_prs(self):
        """Test list_prs function."""
        if self.skip_github_tests:
            print "Skipping test_list_prs, no GitHub token available?"
            return

        parameters = ('closed', 'created', 'asc')

        init_config(build_options={'pr_target_account': GITHUB_USER,
                                   'pr_target_repo': GITHUB_REPO})

        expected = "PR #1: a pr"

        output = gh.list_prs(parameters, per_page=1, github_user=GITHUB_TEST_ACCOUNT)
        self.assertEqual(expected, output)

    def test_reasons_for_closing(self):
        """Test reasons_for_closing function."""
        if self.skip_github_tests:
            print "Skipping test_reasons_for_closing, no GitHub token available?"
            return

        repo_owner = gh.GITHUB_EB_MAIN
        repo_name = gh.GITHUB_EASYCONFIGS_REPO

        build_options = {
            'dry_run': True,
            'github_user': GITHUB_TEST_ACCOUNT,
            'pr_target_account': repo_owner,
            'pr_target_repo': repo_name,
            'robot_path': [],
        }
        init_config(build_options=build_options)

        pr_data, _ = gh.fetch_pr_data(1844, repo_owner, repo_name, GITHUB_TEST_ACCOUNT, full=True)

        self.mock_stdout(True)
        self.mock_stderr(True)
        # can't easily check return value, since auto-detected reasons may change over time if PR is touched
        res = gh.reasons_for_closing(pr_data)
        stdout = self.get_stdout()
        stderr = self.get_stderr()
        self.mock_stdout(False)
        self.mock_stderr(False)

        self.assertTrue(isinstance(res, list))
        self.assertEqual(stderr.strip(), "WARNING: Using easyconfigs from closed PR #1844")
        patterns = [
            "Status of last commit is SUCCESS",
            "Last comment on",
            "No activity since",
            "* QEMU-2.4.0",
        ]
        for pattern in patterns:
            self.assertTrue(pattern in stdout, "Pattern '%s' found in: %s" % (pattern, stdout))

    def test_close_pr(self):
        """Test close_pr function."""
        if self.skip_github_tests:
            print "Skipping test_close_pr, no GitHub token available?"
            return

        build_options = {
            'dry_run': True,
            'github_user': GITHUB_TEST_ACCOUNT,
            'pr_target_account': GITHUB_USER,
            'pr_target_repo': GITHUB_REPO,
        }
        init_config(build_options=build_options)

        self.mock_stdout(True)
        gh.close_pr(2, 'just a test')
        stdout = self.get_stdout()
        self.mock_stdout(False)

        patterns = [
            "hpcugent/testrepository PR #2 was submitted by migueldiascosta",
            "[DRY RUN] Adding comment to testrepository issue #2: '" +
            "@migueldiascosta, this PR is being closed for the following reason(s): just a test",
            "[DRY RUN] Closed hpcugent/testrepository pull request #2",
        ]
        for pattern in patterns:
            self.assertTrue(pattern in stdout, "Pattern '%s' found in: %s" % (pattern, stdout))

    def test_fetch_easyconfigs_from_pr(self):
        """Test fetch_easyconfigs_from_pr function."""
        if self.skip_github_tests:
            print "Skipping test_fetch_easyconfigs_from_pr, no GitHub token available?"
            return

        init_config(build_options={
            'pr_target_account': gh.GITHUB_EB_MAIN,
        })

        # PR for rename of ffmpeg to FFmpeg,
        # see https://github.com/easybuilders/easybuild-easyconfigs/pull/2481/files
        all_ecs_pr2481 = [
            'FFmpeg-2.4-intel-2014.06.eb',
            'FFmpeg-2.4-intel-2014b.eb',
            'FFmpeg-2.8-intel-2015b.eb',
            'OpenCV-2.4.9-intel-2014.06.eb',
            'OpenCV-2.4.9-intel-2014b.eb',
            'animation-2.4-intel-2015b-R-3.2.1.eb',
        ]
        # PR where also files are patched in test/
        # see https://github.com/easybuilders/easybuild-easyconfigs/pull/6587/files
        all_ecs_pr6587 = [
            'WIEN2k-18.1-foss-2018a.eb',
            'WIEN2k-18.1-gimkl-2017a.eb',
            'WIEN2k-18.1-intel-2018a.eb',
            'libxc-4.2.3-foss-2018a.eb',
            'libxc-4.2.3-gimkl-2017a.eb',
            'libxc-4.2.3-intel-2018a.eb',
        ]
        # PR where files are renamed
        # see https://github.com/easybuilders/easybuild-easyconfigs/pull/7159/files
        all_ecs_pr7159 = [
            'DOLFIN-2018.1.0.post1-foss-2018a-Python-3.6.4.eb',
            'OpenFOAM-5.0-20180108-foss-2018a.eb',
            'OpenFOAM-5.0-20180108-intel-2018a.eb',
            'OpenFOAM-6-foss-2018b.eb',
            'OpenFOAM-6-intel-2018a.eb',
            'OpenFOAM-v1806-foss-2018b.eb',
            'PETSc-3.9.3-foss-2018a.eb',
            'SCOTCH-6.0.6-foss-2018a.eb',
            'SCOTCH-6.0.6-foss-2018b.eb',
            'SCOTCH-6.0.6-intel-2018a.eb',
            'Trilinos-12.12.1-foss-2018a-Python-3.6.4.eb'
        ]

        for pr, all_ecs in [(2481, all_ecs_pr2481), (6587, all_ecs_pr6587), (7159, all_ecs_pr7159)]:
            try:
                tmpdir = os.path.join(self.test_prefix, 'pr%s' % pr)
                ec_files = gh.fetch_easyconfigs_from_pr(pr, path=tmpdir, github_user=GITHUB_TEST_ACCOUNT)
                self.assertEqual(sorted(all_ecs), sorted([os.path.basename(f) for f in ec_files]))
            except URLError, err:
                print "Ignoring URLError '%s' in test_fetch_easyconfigs_from_pr" % err

    def test_fetch_latest_commit_sha(self):
        """Test fetch_latest_commit_sha function."""
        if self.skip_github_tests:
            print "Skipping test_fetch_latest_commit_sha, no GitHub token available?"
            return

        sha = gh.fetch_latest_commit_sha('easybuild-framework', 'easybuilders', github_user=GITHUB_TEST_ACCOUNT)
        self.assertTrue(re.match('^[0-9a-f]{40}$', sha))
        sha = gh.fetch_latest_commit_sha('easybuild-easyblocks', 'easybuilders', github_user=GITHUB_TEST_ACCOUNT,
                                         branch='develop')
        self.assertTrue(re.match('^[0-9a-f]{40}$', sha))

    def test_download_repo(self):
        """Test download_repo function."""
        if self.skip_github_tests:
            print "Skipping test_download_repo, no GitHub token available?"
            return

        # default: download tarball for master branch of easybuilders/easybuild-easyconfigs repo
        path = gh.download_repo(path=self.test_prefix, github_user=GITHUB_TEST_ACCOUNT)
        repodir = os.path.join(self.test_prefix, 'easybuilders', 'easybuild-easyconfigs-master')
        self.assertTrue(os.path.samefile(path, repodir))
        self.assertTrue(os.path.exists(repodir))
        shafile = os.path.join(repodir, 'latest-sha')
        self.assertTrue(re.match('^[0-9a-f]{40}$', read_file(shafile)))
        self.assertTrue(os.path.exists(os.path.join(repodir, 'easybuild', 'easyconfigs', 'f', 'foss', 'foss-2015a.eb')))

        # existing downloaded repo is not reperformed, except if SHA is different
        account, repo, branch = 'boegel', 'easybuild-easyblocks', 'develop'
        repodir = os.path.join(self.test_prefix, account, '%s-%s' % (repo, branch))
        latest_sha = gh.fetch_latest_commit_sha(repo, account, branch=branch, github_user=GITHUB_TEST_ACCOUNT)

        # put 'latest-sha' fail in place, check whether repo was (re)downloaded (should not)
        shafile = os.path.join(repodir, 'latest-sha')
        write_file(shafile, latest_sha)
        path = gh.download_repo(repo=repo, branch=branch, account=account, path=self.test_prefix,
                                github_user=GITHUB_TEST_ACCOUNT)
        self.assertTrue(os.path.samefile(path, repodir))
        self.assertEqual(os.listdir(repodir), ['latest-sha'])

        # remove 'latest-sha' file and verify that download was performed
        os.remove(shafile)
        path = gh.download_repo(repo=repo, branch=branch, account=account, path=self.test_prefix,
                                github_user=GITHUB_TEST_ACCOUNT)
        self.assertTrue(os.path.samefile(path, repodir))
        self.assertTrue('easybuild' in os.listdir(repodir))
        self.assertTrue(re.match('^[0-9a-f]{40}$', read_file(shafile)))
        self.assertTrue(os.path.exists(os.path.join(repodir, 'easybuild', 'easyblocks', '__init__.py')))

    def test_install_github_token(self):
        """Test for install_github_token function."""
        if self.skip_github_tests:
            print "Skipping test_install_github_token, no GitHub token available?"
            return

        if not HAVE_KEYRING:
            print "Skipping test_install_github_token, keyring module not available"
            return

        random_user = ''.join(random.choice(string.letters) for _ in range(10))
        self.assertEqual(gh.fetch_github_token(random_user), None)

        # poor mans mocking of getpass
        # inject leading/trailing spaces to verify stripping of provided value
        def fake_getpass(*args, **kwargs):
            return ' ' + self.github_token + '  '

        orig_getpass = gh.getpass.getpass
        gh.getpass.getpass = fake_getpass

        token_installed = False
        try:
            gh.install_github_token(random_user, silent=True)
            token_installed = True
        except Exception as err:
            print err

        gh.getpass.getpass = orig_getpass

        token = gh.fetch_github_token(random_user)

        # cleanup
        if token_installed:
            keyring.delete_password(gh.KEYRING_GITHUB_TOKEN, random_user)

        # deliberately not using assertEqual, keep token secret!
        self.assertTrue(token_installed)
        self.assertTrue(token == self.github_token)

    def test_validate_github_token(self):
        """Test for validate_github_token function."""
        if self.skip_github_tests:
            print "Skipping test_validate_github_token, no GitHub token available?"
            return

        if not HAVE_KEYRING:
            print "Skipping test_validate_github_token, keyring module not available"
            return

        self.assertTrue(gh.validate_github_token(self.github_token, GITHUB_TEST_ACCOUNT))

    def test_find_easybuild_easyconfig(self):
        """Test for find_easybuild_easyconfig function"""
        if self.skip_github_tests:
            print "Skipping test_find_easybuild_easyconfig, no GitHub token available?"
            return
        path = gh.find_easybuild_easyconfig(github_user=GITHUB_TEST_ACCOUNT)
        expected = os.path.join('e', 'EasyBuild', 'EasyBuild-[1-9]+\.[0-9]+\.[0-9]+\.eb')
        regex = re.compile(expected)
        self.assertTrue(regex.search(path), "Pattern '%s' found in '%s'" % (regex.pattern, path))
        self.assertTrue(os.path.exists(path), "Path %s exists" % path)

    def test_find_patches(self):
        """ Test for find_software_name_for_patch """
        testdir = os.path.dirname(os.path.abspath(__file__))
        ec_path = os.path.join(testdir, 'easyconfigs')
        init_config(build_options={
            'allow_modules_tool_mismatch': True,
            'minimal_toolchains': True,
            'use_existing_modules': True,
            'external_modules_metadata': ConfigObj(),
            'robot_path': [ec_path],
            'valid_module_classes': module_classes(),
            'validate': False,
        })
        self.mock_stdout(True)
        ec = gh.find_software_name_for_patch('toy-0.0_fix-silly-typo-in-printf-statement.patch')
        txt = self.get_stdout()
        self.mock_stdout(False)

        self.assertTrue(ec == 'toy')
        reg = re.compile(r'[1-9]+ of [1-9]+ easyconfigs checked')
        self.assertTrue(re.search(reg, txt))

    def test_check_pr_eligible_to_merge(self):
        """Test check_pr_eligible_to_merge function"""
        def run_check(expected_result=False):
            """Helper function to check result of check_pr_eligible_to_merge"""
            self.mock_stdout(True)
            self.mock_stderr(True)
            res = gh.check_pr_eligible_to_merge(pr_data)
            stdout = self.get_stdout()
            stderr = self.get_stderr()
            self.mock_stdout(False)
            self.mock_stderr(False)
            self.assertEqual(res, expected_result)
            self.assertEqual(stdout, expected_stdout)
            self.assertTrue(expected_warning in stderr, "Found '%s' in: %s" % (expected_warning, stderr))
            return stderr

        pr_data = {
            'base': {
                'ref': 'master',
                'repo': {
                    'name': 'easybuild-easyconfigs',
                    'owner': {'login': 'easybuilders'},
                },
            },
            'status_last_commit': None,
            'issue_comments': [],
            'milestone': None,
            'number': '1234',
            'reviews': [],
        }

        test_result_warning_template = "* test suite passes: %s => not eligible for merging!"

        expected_stdout = "Checking eligibility of easybuilders/easybuild-easyconfigs PR #1234 for merging...\n"

        # target branch for PR must be develop
        expected_warning = "* targets develop branch: FAILED; found 'master' => not eligible for merging!\n"
        run_check()

        pr_data['base']['ref'] = 'develop'
        expected_stdout += "* targets develop branch: OK\n"

        # test suite must PASS (not failed, pending or unknown) in Travis
        tests = [
            ('', '(result unknown)'),
            ('foobar', '(result unknown)'),
            ('pending', 'pending...'),
            ('error', 'FAILED'),
            ('failure', 'FAILED'),
        ]
        for status, test_result in tests:
            pr_data['status_last_commit'] = status
            expected_warning = test_result_warning_template % test_result
            run_check()

        pr_data['status_last_commit'] = 'success'
        expected_stdout += "* test suite passes: OK\n"
        expected_warning = ''
        run_check()

        # at least the last test report must be successful (and there must be one)
        expected_warning = "* last test report is successful: (no test reports found) => not eligible for merging!"
        run_check()

        pr_data['issue_comments'] = [
            {'body': "@easybuild-easyconfigs/maintainers: please review/merge?"},
            {'body': "Test report by @boegel\n**FAILED**\nnothing ever works..."},
            {'body': "this is just a regular comment"},
        ]
        expected_warning = "* last test report is successful: FAILED => not eligible for merging!"
        run_check()

        pr_data['issue_comments'].extend([
            {'body': "yet another comment"},
            {'body': "Test report by @boegel\n**SUCCESS**\nit's all good!"},
        ])
        expected_stdout += "* last test report is successful: OK\n"
        expected_warning = ''
        run_check()

        # approved style review by a human is required
        expected_warning = "* approved review: MISSING => not eligible for merging!"
        run_check()

        pr_data['issue_comments'].insert(2, {'body': 'lgtm'})
        run_check()

        pr_data['reviews'].append({'state': 'CHANGES_REQUESTED', 'user': {'login': 'boegel'}})
        run_check()

        pr_data['reviews'].append({'state': 'APPROVED', 'user': {'login': 'boegel'}})
        expected_stdout += "* approved review: OK (by boegel)\n"
        expected_warning = ''
        run_check()

        # milestone must be set
        expected_warning = "* milestone is set: no milestone found => not eligible for merging!"
        run_check()

        pr_data['milestone'] = {'title': '3.3.1'}
        expected_stdout += "* milestone is set: OK (3.3.1)\n"

        # all checks pass, PR is eligible for merging
        expected_warning = ''
        self.assertEqual(run_check(True), '')

    def test_det_patch_specs(self):
        """Test for det_patch_specs function."""

        # robot_path build option is used by find_software_name_for_patch, which is called by det_patch_specs
        init_config(build_options={'robot_path': []})

        patch_paths = [os.path.join(self.test_prefix, p) for p in ['1.patch', '2.patch', '3.patch']]
        file_info = {'ecs': [
                {'name': 'A', 'patches': ['1.patch']},
                {'name': 'B', 'patches': []},
            ]
        }
        error_pattern = "Failed to determine software name to which patch file .*/2.patch relates"
        self.mock_stdout(True)
        self.assertErrorRegex(EasyBuildError, error_pattern, gh.det_patch_specs, patch_paths, file_info)
        self.mock_stdout(False)

        file_info['ecs'].append({'name': 'C', 'patches': [('3.patch', 'subdir'), '2.patch']})
        self.mock_stdout(True)
        res = gh.det_patch_specs(patch_paths, file_info)
        self.mock_stdout(False)

        self.assertEqual(len(res), 3)
        self.assertEqual(os.path.basename(res[0][0]), '1.patch')
        self.assertEqual(res[0][1], 'A')
        self.assertEqual(os.path.basename(res[1][0]), '2.patch')
        self.assertEqual(res[1][1], 'C')
        self.assertEqual(os.path.basename(res[2][0]), '3.patch')
        self.assertEqual(res[2][1], 'C')


def suite():
    """ returns all the testcases in this module """
    return TestLoaderFiltered().loadTestsFromTestCase(GithubTest, sys.argv[1:])


if __name__ == '__main__':
    TextTestRunner(verbosity=1).run(suite())
