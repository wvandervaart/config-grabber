# -----------------------
# this is another version that includes the is_insecure() function to check that the file is chmod 600.
import os
import sys
import stat
def is_insecure(filepath):
    """Check if file is accessible by group or other."""
    st = os.stat(filepath)
    if st.st_mode & stat.S_IRGRP:
        return True
    if st.st_mode & stat.S_IWGRP:
        return True
    if st.st_mode & stat.S_IXGRP:
        return True
    if st.st_mode & stat.S_IROTH:
        return True
    if st.st_mode & stat.S_IWOTH:
        return True
    if st.st_mode & stat.S_IXOTH:
        return True
    return False

def get(TARGET):
    """Read token from ~/.token_TARGET."""
    token_file = '{}/.token_{}'.format(os.environ['HOME'], TARGET)

    if is_insecure(token_file):
        print('Token file is insecure, please chmod 600 {}'.format(token_file))
        print('EXITING')
        sys.exit(1)
    try:
        with open(token_file) as f:
            token = f.read().splitlines()
            token = token[0].strip()
    except IOError as e:
        print("{} config file is missing or cannot be read.".format(TARGET))
        token = None
    return token