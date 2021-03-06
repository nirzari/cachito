# SPDX-License-Identifier: GPL-3.0-or-later

from pathlib import Path
import random
import shutil
import string

import git
import pytest

import utils


def create_local_repository(repo_path):
    """
    Create a local git repoitory.

    :param str repo_path: path to new bare git repository
    :return: normalized bare git repository path
    :rtype: str
    """
    bare_repo_dir = Path(repo_path)
    bare_repo = git.Repo.init(str(bare_repo_dir), bare=True)
    assert bare_repo.bare, f"{bare_repo} is not bare repository"
    # We need to expand this for later usage from the original repo directory
    return str(bare_repo_dir.resolve())


class TestCachedDependencies:
    """Test class for cached dependencies."""

    @pytest.fixture(autouse=True)
    def setup_method_fixture(self, test_env):
        """Create bare git repo and a pool for removing shared directories."""
        self.directories = []
        self.env_data = utils.load_test_data("cached_dependencies.yaml")
        self.git_user = self.env_data["test_repo"].get("git_user")
        self.git_email = self.env_data["test_repo"].get("git_email")
        if self.env_data["test_repo"].get("use_local"):
            repo_path = create_local_repository(self.env_data["test_repo"]["ssh_url"])
            self.env_data["test_repo"]["ssh_url"] = repo_path
            # Defer cleanups
            self.directories.append(repo_path)

    def teardown_method(self, method):
        """Remove shared directories in the pool."""
        for directory in self.directories:
            shutil.rmtree(directory)

    def test_using_cached_dependencies(self, tmpdir, test_env):
        """
        Check that the cached dependencies are used instead of downloading them from repo again.

        Preconditions:
        * On git instance prepare an empty repository

        Process:
        * Clone the package from the upstream repository
        * Create empty commit on new test branch and push it to the prepared repository
        * Send new request to Cachito API which would fetch data from the prepared repository
        * Delete branch with the corresponding commit
        * Send the same request to Cachito API

        Checks:
        * Check that the state of the first request is complete
        * Check that the commit is not available in the repository after the branch is deleted
        * Check that the state of the second request is complete
        """
        generated_suffix = "".join(
            random.choice(string.ascii_letters + string.digits) for x in range(10)
        )
        branch_name = f"test-{generated_suffix}"
        repo = git.repo.Repo.clone_from(self.env_data["seed_repo"]["https_url"], tmpdir)
        remote = repo.create_remote("test", url=self.env_data["test_repo"]["ssh_url"])
        assert remote.exists(), f"Remote {remote.name} does not exist"

        # set user configuration, if available
        if self.git_user:
            repo.config_writer().set_value("user", "name", self.git_user).release()
        if self.git_email:
            repo.config_writer().set_value("user", "email", self.git_email).release()

        try:
            repo.create_head(branch_name).checkout()
            repo.git.commit("--allow-empty", m="Commit created in integration test for Cachito")
            repo.git.push("-u", remote.name, branch_name)
            commit = repo.head.commit.hexsha

            client = utils.Client(
                test_env["api_url"], test_env["api_auth_type"], test_env.get("timeout"),
            )
            response = client.create_new_request(
                payload={
                    "repo": self.env_data["test_repo"]["https_url"],
                    "ref": commit,
                    "pkg_managers": self.env_data["test_repo"]["pkg_managers"],
                },
            )
            first_response = client.wait_for_complete_request(response)
            utils.assert_properly_completed_response(first_response)
            assert repo.git.branch(
                "-a", "--contains", commit
            ), f"Commit {commit} is not in branches (it should be there)."

        finally:
            repo.git.push("--delete", remote.name, branch_name)

        repo.heads.master.checkout()
        repo.git.branch("-D", branch_name)
        assert not repo.git.branch(
            "-a", "--contains", commit
        ), f"Commit {commit} is still in a branch (it shouldn't be there at this point)."

        response = client.create_new_request(
            payload={
                "repo": self.env_data["test_repo"]["https_url"],
                "ref": commit,
                "pkg_managers": self.env_data["test_repo"]["pkg_managers"],
            },
        )
        second_response = client.wait_for_complete_request(response)
        utils.assert_properly_completed_response(second_response)
        assert first_response.data["ref"] == second_response.data["ref"]
        assert first_response.data["repo"] == second_response.data["repo"]
        assert set(first_response.data["pkg_managers"]) == set(second_response.data["pkg_managers"])
        first_pkgs = utils.make_list_of_packages_hashable(first_response.data["packages"])
        second_pkgs = utils.make_list_of_packages_hashable(second_response.data["packages"])
        assert first_pkgs == second_pkgs
        first_deps = utils.make_list_of_packages_hashable(first_response.data["dependencies"])
        second_deps = utils.make_list_of_packages_hashable(second_response.data["dependencies"])
        assert first_deps == second_deps
