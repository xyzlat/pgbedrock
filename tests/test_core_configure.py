import copy
import os

import pytest
import yaml

from pgbedrock import core_configure
from pgbedrock import ownerships as own
from conftest import Q_GET_ROLE_ATTRIBUTE, NEW_USER, run_setup_sql
from pgbedrock import attributes as attr
from test_ownerships import Q_SCHEMA_EXISTS
from test_memberships import Q_HAS_ROLE
from test_privileges import Q_HAS_PRIVILEGE


@pytest.fixture
def set_envvar(request):
    """ Set an environment variable. We use a fixture to ensure cleanup if the test fails """
    k, v = request.param
    os.environ[k] = v
    yield
    del os.environ[k]


def test_render_template(tmpdir):
    spec_path = tmpdir.join('spec.yml')
    spec_path.write("""
        fred:
          can_login: yes

        my_group:
          can_login: no

        admin:
          can_login: yes
          is_superuser: yes
          options:
              - CREATEDB
              - CREATEROLE
              - REPLICATION

        service1:
          can_login: yes
          schemas:
              - service1_schema
    """)
    spec = core_configure.render_template(spec_path.strpath)
    spec = yaml.load(spec)

    assert len(spec) == 4
    assert set(spec.keys()) == {'admin', 'my_group', 'service1', 'fred'}


@pytest.mark.parametrize('set_envvar', [('FRED_PASSWORD', 'a_password')], indirect=True)
def test_load_spec_with_templated_variables(tmpdir, set_envvar):
    spec_path = tmpdir.join('spec.yml')
    spec_path.write("""
        fred:
          can_login: yes
          options:
            - PASSWORD: "{{ env['FRED_PASSWORD'] }}"
    """)
    spec = core_configure.render_template(spec_path.strpath)
    spec = yaml.load(spec)

    password_option = spec['fred']['options'][0]
    assert password_option['PASSWORD'] == 'a_password'


def test_load_spec_fails_missing_templated_envvars(capsys, tmpdir):
    envvar_name = 'MISSING_ENVVAR'
    assert envvar_name not in os.environ

    spec = """
        fred:
          can_login: yes
          options:
            - PASSWORD: "{{ env['%s'] }}"
    """ % envvar_name
    spec_path = tmpdir.join('spec.yml')
    spec_path.write(spec)

    with pytest.raises(SystemExit):
        core_configure.render_template(spec_path.strpath)

    out, err = capsys.readouterr()
    expected = core_configure.MISSING_ENVVAR_MSG.format('')
    assert expected in out
    assert envvar_name in out


def test_load_spec_fails_file_not_found(capsys):
    filename = 'non_existent.yml'
    dirname = os.path.dirname(__file__)
    path = os.path.join(dirname, filename)

    with pytest.raises(SystemExit):
        core_configure.render_template(path)

    out, _ = capsys.readouterr()
    assert core_configure.FILE_OPEN_ERROR_MSG.format(path, '') in out


def test_verify_spec_fails(capsys):
    """ We could check more functionality, but at that point we'd just be testing cerberus. This
    test is just to verify that a failure will happen and will be presented as we'd expect """
    spec_yaml = """
        fred:
            attribute:
                - flub
        """
    spec = yaml.load(spec_yaml)
    errors = core_configure.verify_schema(spec)
    expected = core_configure.VALIDATION_ERR_MSG.format('fred', 'attribute', 'unknown field')
    assert expected == errors[0]


def test_verify_spec_succeeds(capsys):
    spec_yaml = """
        fred:
            attributes:
                - flub

        mark:
        """
    spec = yaml.load(spec_yaml)
    errors = core_configure.verify_schema(spec)
    assert len(errors) == 0


def test_verify_spec_fails_multiple_roles_own_schema(capsys):
    spec_yaml = """
    jfinance:
        owns:
            schemas:
                - finance_documents
    jfauxnance:
        owns:
            schemas:
                - finance_documents
    """
    spec = yaml.load(spec_yaml)
    errors = core_configure.check_for_multi_schema_owners(spec)
    expected = core_configure.MULTIPLE_SCHEMA_OWNER_ERR_MSG.format('finance_documents', 'jfinance, jfauxnance')
    assert [expected] == errors


def test_verify_spec_fails_multiple_roles_own_schema_personal_schema(capsys):
    spec_yaml = """
    jfinance:
        has_personal_schema: yes
        owns:
            schemas:
                - finance_documents
    jfauxnance:
        owns:
            schemas:
                - jfinance
    """
    spec = yaml.load(spec_yaml)
    errors = core_configure.check_for_multi_schema_owners(spec)
    expected = core_configure.MULTIPLE_SCHEMA_OWNER_ERR_MSG.format('jfinance', 'jfinance, jfauxnance')
    assert [expected] == errors


def test_verify_spec_fails_role_defined_multiple_times(tmpdir, capsys):
    spec_path = tmpdir.join('spec.yml')
    spec_path.write("""
    jfinance:
        owns:
            schemas:
                - finance_documents
    jfinance:
        owns:
            schemas:
                - even_more_finance_documents
    patty:
        owns:
            schemas:
                - tupperwear
    """)
    rendered_template = core_configure.render_template(spec_path.strpath)
    errors = core_configure.detect_multiple_role_definitions(rendered_template)
    expected = core_configure.DUPLICATE_ROLE_DEFINITIONS_ERR_MSG.format('jfinance')
    assert [expected] == errors


def test_verify_spec_fails_object_referenced_read_write(capsys):
    spec_yaml = """
    margerie:
        can_login: true
        privileges:
            {}:
                read:
                    - big_bad
                write:
                    - big_bad
    danil:
        can_login: true
        privileges:
            sequences:
                read:
                    - hoop
                write:
                    - grok
    """

    privilege_types = ('schemas', 'sequences', 'tables')
    for t in privilege_types:
        spec = yaml.load(spec_yaml.format(t))
        errors = core_configure.check_read_write_obj_references(spec)
        err_string = "margerie: {'%s': ['big_bad']}" % t
        expected = core_configure.OBJECT_REF_READ_WRITE_ERR.format(err_string)
        assert [expected] == errors


@pytest.mark.parametrize('statements, expected', [
    (['--foo', '--bar'], False),
    (['--foo', 'bar'], True),
    ([], False),
])
def test_has_changes(statements, expected):
    assert core_configure.has_changes(statements) is expected


@pytest.mark.usefixtures('drop_users_and_objects')
def test_configure_no_changes_needed(tmpdir, capsys, db_config):
    """
    We add a new user (NEW_USER) through pgbedrock and make sure that 1) this change isn't
    committed if we pass --check and 2) this change _is_ committed if we pass --live
    """

    spec_path = tmpdir.join('spec.yml')
    spec_path.write("""
    postgres:
        is_superuser: yes
        owns:
            schemas:
                - information_schema
                - pg_catalog
                - public

    test_user:
        can_login: yes
        is_superuser: yes
        attributes:
            - PASSWORD "test_password"
    """.format(new_user=NEW_USER))

    params = copy.deepcopy(db_config)
    params.update(
        dict(spec=spec_path.strpath,
             prompt=False,
             attributes=True,
             memberships=True,
             ownerships=True,
             privileges=True,
             live=False,
             verbose=False
             )
    )
    core_configure.configure(**params)
    out, err = capsys.readouterr()
    assert core_configure.SUCCESS_MSG in out


@pytest.mark.usefixtures('drop_users_and_objects')
@pytest.mark.parametrize('live_mode, expected', [(True, 1), (False, 0)])
def test_configure_live_mode_works(capsys, cursor, tiny_spec, db_config, live_mode, expected):
    """
    We add a new user (NEW_USER) through pgbedrock and make sure that 1) this change isn't
    committed if we pass --check and 2) this change _is_ committed if we pass --live
    """
    # Assert that we start without the role we are trying to add
    cursor.execute(Q_GET_ROLE_ATTRIBUTE.format('rolname', NEW_USER))
    assert cursor.rowcount == 0

    params = copy.deepcopy(db_config)
    params.update(
        dict(spec=tiny_spec,
             prompt=False,
             attributes=True,
             memberships=True,
             ownerships=True,
             privileges=True,
             live=live_mode,
             verbose=False
             )
    )
    core_configure.configure(**params)
    out, err = capsys.readouterr()

    # We want to make sure that in live mode changes from each module have been
    # made (and additionally that in check mode these changes were _not_ made)
    cursor.execute(Q_GET_ROLE_ATTRIBUTE.format('rolname', NEW_USER))
    assert cursor.rowcount == expected

    cursor.execute(Q_SCHEMA_EXISTS.format(NEW_USER))
    assert cursor.rowcount == expected

    if live_mode:
        cursor.execute(Q_HAS_ROLE.format(NEW_USER, 'postgres'))
        assert cursor.rowcount == expected

        cursor.execute(Q_HAS_PRIVILEGE.format(NEW_USER, 'pg_catalog.pg_class'))
        assert cursor.fetchone()[0] is True


@pytest.mark.usefixtures('drop_users_and_objects')
def test_configure_live_does_not_leak_passwords(tmpdir, capsys, cursor, db_config):
    """
    We add a new user (NEW_USER) through pgbedrock and make sure that 1) this change isn't
    committed if we pass --check and 2) this change _is_ committed if we pass --live
    """
    # Assert that we start without the role we are trying to add
    cursor.execute(Q_GET_ROLE_ATTRIBUTE.format('rolname', NEW_USER))
    assert cursor.rowcount == 0

    new_password = 'supersecret'
    spec_path = tmpdir.join('spec.yml')
    spec_path.write("""
    postgres:
        is_superuser: yes
        owns:
            schemas:
                - information_schema
                - pg_catalog
                - public

    test_user:
        can_login: yes
        is_superuser: yes
        attributes:
            - PASSWORD "test_password"

    {new_user}:
        attributes:
            - PASSWORD "{new_password}"
    """.format(new_user=NEW_USER, new_password=new_password))

    params = copy.deepcopy(db_config)
    params.update(
        dict(spec=spec_path.strpath,
             prompt=False,
             attributes=True,
             memberships=True,
             ownerships=True,
             privileges=True,
             live=True,
             verbose=True,
             )
    )
    core_configure.configure(**params)

    # Verify that the password was changed
    new_md5_hash = attr.create_md5_hash(NEW_USER, new_password)
    cursor.execute("SELECT rolpassword FROM pg_authid WHERE rolname = '{}';".format(NEW_USER))
    assert cursor.fetchone()[0] == new_md5_hash

    # Verify that the password isn't exposed in our output
    out, err = capsys.readouterr()
    assert 'supersecret' not in out
    assert 'supersecret' not in err

    # Verify that the sanitized record of the password change is in our output
    assert 'ALTER ROLE "foobar" WITH ENCRYPTED PASSWORD \'******\';' in out


@run_setup_sql([
    attr.Q_CREATE_ROLE.format(NEW_USER),
    attr.Q_ALTER_PASSWORD.format(NEW_USER, 'some_password'),
])
@pytest.mark.usefixtures('drop_users_and_objects')
def test_no_password_attribute_makes_password_none(capsys, cursor, tiny_spec, db_config):

    # We have to commit the changes from @run_setup_sql so they will be seen by
    # the transaction generated within pgbedrock configure
    cursor.connection.commit()

    # Assert that we start with the role whose password we are trying to modify
    cursor.execute(Q_GET_ROLE_ATTRIBUTE.format('rolname', NEW_USER))
    assert cursor.rowcount == 1

    # Assert that the password is not NULL
    cursor.execute("SELECT rolpassword IS NOT NULL FROM pg_authid WHERE rolname = '{}'".format(NEW_USER))
    assert cursor.fetchone()[0] is True

    params = copy.deepcopy(db_config)
    params.update(
        dict(spec=tiny_spec,
             prompt=False,
             attributes=True,
             memberships=True,
             ownerships=True,
             privileges=True,
             live=True,
             verbose=False
             )
    )
    core_configure.configure(**params)
    out, err = capsys.readouterr()

    # Assert that the password is NULL now
    cursor.execute("SELECT rolpassword IS NULL FROM pg_authid WHERE rolname = '{}'".format(NEW_USER))
    assert cursor.fetchone()[0] is True


def test_configure_schema_role_has_dash(tmpdir, capsys, db_config):
    """
    We add a new user ('role-with-dash') through pgbedrock and make sure that that user can create
    a personal schema
    """
    role = 'role-with-dash'

    spec_path = tmpdir.join('spec.yml')
    spec_path.write("""
    postgres:
        is_superuser: yes
        owns:
            schemas:
                - information_schema
                - pg_catalog
                - public

    test_user:
        can_login: yes
        is_superuser: yes
        attributes:
            - PASSWORD "test_password"

    {}:
        has_personal_schema: yes
    """.format(role))

    params = copy.deepcopy(db_config)
    params.update(
        dict(spec=spec_path.strpath,
             prompt=False,
             attributes=True,
             memberships=True,
             ownerships=True,
             privileges=True,
             live=False,
             verbose=False
             )
    )
    core_configure.configure(**params)
    out, err = capsys.readouterr()
    assert own.Q_CREATE_SCHEMA.format(role, role) in out
