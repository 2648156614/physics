from config import ALLOWED_EXTENSIONS


def allowed_file(filename):
    """检查文件扩展名是否允许。"""
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS


def allowed_excel_file(filename):
    """检查是否为支持的 Excel 文件。"""
    return '.' in filename and filename.rsplit('.', 1)[1].lower() == 'xlsx'
