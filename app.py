from flask import Flask, request, jsonify, session, redirect, render_template, send_file
import psycopg2
import os
from datetime import datetime
import pandas as pd
from io import BytesIO
import warnings

warnings.filterwarnings('ignore', category=UserWarning)
from dotenv import load_dotenv

# .env ဖိုင်ထဲက Data များကို ဆွဲယူမည်
load_dotenv()

app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", "fallback_secret_key")

DB_URI = os.getenv("DATABASE_URL")
if not DB_URI:
    raise ValueError("ERROR: .env ဖိုင်ထဲတွင် DATABASE_URL ကို မတွေ့ပါ။")

def get_db_connection():
    return psycopg2.connect(DB_URI)

# ==========================================
# အစဉ်လိုက် Form/Voucher နံပါတ်များ ဖန်တီးပေးသော Function
# ==========================================
def generate_serial(prefix, table_name, column_name, padding=3):
    conn = get_db_connection()
    cur = conn.cursor()
    try:
        query = f"SELECT {column_name} FROM {table_name} WHERE {column_name} LIKE %s ORDER BY {column_name} DESC LIMIT 1"
        cur.execute(query, (f"{prefix}-%",))
        result = cur.fetchone()
        
        if result and result[0]:
            try:
                last_num = int(result[0].split('-')[1])
                new_num = last_num + 1
            except:
                new_num = 1
        else:
            new_num = 1
            
        return f"{prefix}-{new_num:0{padding}d}"
    except Exception as e:
        print("Serial Gen Error:", e)
        return f"{prefix}-" + datetime.now().strftime("%H%M%S")
    finally:
        cur.close()
        conn.close()

# ==========================================
# ၁။ Login နှင့် User Management
# ==========================================
@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        data = request.json
        username = data.get('username')
        password = data.get('password')
        
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("SELECT Role FROM Users WHERE Username = %s AND Password = %s", (username, password))
        user = cur.fetchone()
        cur.close()
        conn.close()
        
        if user:
            session['user'] = username
            session['role'] = user[0] 
            return jsonify({"status": "success", "role": user[0], "message": "Login အောင်မြင်ပါသည်"})
        else:
            return jsonify({"status": "error", "message": "Username သို့မဟုတ် Password မှားနေပါသည်"})
            
    return render_template('login.html')

@app.route('/logout')
def logout():
    session.clear()
    return redirect('/login')

@app.route('/users', methods=['GET', 'POST'])
def manage_users():
    if 'user' not in session or session.get('role') != 'Admin':
        return "သင့်တွင် ဤမျက်နှာပြင်သို့ ဝင်ရောက်ခွင့် မရှိပါ။", 403

    conn = get_db_connection()
    cur = conn.cursor()

    if request.method == 'POST':
        try:
            target_user = request.form['username']
            new_password = request.form['new_password']
            cur.execute("UPDATE Users SET Password = %s WHERE Username = %s", (new_password, target_user))
            conn.commit()
            return redirect('/users')
        except Exception as e:
            conn.rollback()
            return f"Error updating password: {e}"
        finally:
            cur.close()
            conn.close()

    cur.execute("SELECT Username, Role FROM Users ORDER BY Role, Username")
    user_list = cur.fetchall()
    cur.close()
    conn.close()
    return render_template('users.html', role=session.get('role'), session=session, user_list=user_list)

# ==========================================
# ၂။ Dashboard (ပင်မ မျက်နှာပြင်)
# ==========================================
@app.route('/')
def dashboard():
    if 'user' not in session: return redirect('/login')
        
    role = session.get('role')
    conn = get_db_connection()
    cur = conn.cursor()
    
    try:
        cur.execute("SELECT COALESCE(SUM(Cr_Amount), 0) FROM Finance_Ledger WHERE Account_Head = 'Sales / Income'")
        total_income = cur.fetchone()[0]

        cur.execute("""
            SELECT COALESCE(SUM(Dr_Amount), 0) 
            FROM Finance_Ledger 
            WHERE Account_Head IN ('Site Expense (WIP)', 'General Expense', 'Salary Expense', 'Fuel Expense', 'Office Expense', 'Transportation Expense')
        """)
        total_expense = cur.fetchone()[0]

        cur.execute("SELECT COALESCE(SUM(Qty_In), 0) - COALESCE(SUM(Qty_Out), 0) FROM Inventory_Ledger")
        stock_balance = cur.fetchone()[0]

        cur.execute("SELECT COUNT(*) FROM Purchase_Orders WHERE Status = 'Pending'")
        pending_po = cur.fetchone()[0]

        cur.execute("""
            SELECT Item_Name, SUM(Qty_In) - SUM(Qty_Out) as Balance
            FROM Inventory_Ledger
            GROUP BY Item_Name
            HAVING SUM(Qty_In) - SUM(Qty_Out) > 0
            ORDER BY Balance DESC LIMIT 5
        """)
        stock_data = cur.fetchall()
        stock_labels = [row[0] for row in stock_data]
        stock_values = [int(row[1]) for row in stock_data]
    except Exception as e:
        print("Dashboard Error:", e)
        total_income = total_expense = stock_balance = pending_po = 0
        stock_labels = []
        stock_values = []
    finally:
        cur.close()
        conn.close()

    return render_template('dashboard.html', role=role, session=session,
                           total_income=total_income, total_expense=total_expense,
                           stock_balance=stock_balance, pending_po=pending_po,
                           stock_labels=stock_labels, stock_values=stock_values)

# ==========================================
# ၃။ Inventory (ပစ္စည်းစာရင်း)
# ==========================================
@app.route('/inventory')
def inventory():
    if 'user' not in session: return redirect('/login')
    role = session.get('role')
    if role not in ['Admin', 'Store Keeper']: return "Unauthorized Access", 403

    conn = get_db_connection()
    cur = conn.cursor()
    try:
        cur.execute("""
            SELECT 
                i.Ledger_ID, i.Record_Date, i.Form_Type, i.Form_No, 
                i.Item_Code, i.Item_Name, i.Qty_In, i.Qty_Out, 
                i.From_Location, 
                COALESCE(l.Base_Type || ' (' || l.Project_Custom_Name || ')', i.To_Location) AS Location_Name
            FROM Inventory_Ledger i
            LEFT JOIN Locations l ON i.To_Location = l.Location_ID
            ORDER BY i.Record_Date DESC, i.Ledger_ID DESC
        """)
        items = cur.fetchall()
    except Exception as e:
        print("Inventory Error:", e)
        items = []
    finally:
        cur.close()
        conn.close()

    return render_template('inventory.html', role=role, session=session, items=items)

# ==========================================
# ၄။ Finance Vouchers (ငွေစာရင်း)
# ==========================================
@app.route('/finance')
def finance():
    if 'user' not in session: return redirect('/login')
    role = session.get('role')
    if role not in ['Admin', 'Finance']: return "Unauthorized Access", 403

    filter_type = request.args.get('filter')
    conn = get_db_connection()
    cur = conn.cursor()
    try:
        base_query = """
            SELECT 
                f.Ledger_ID, f.Record_Date, f.Voucher_Type, f.Voucher_No, 
                f.Description, f.Account_Head, f.Dr_Amount, f.Cr_Amount, 
                f.Project_Location,
                COALESCE(l.Base_Type || ' (' || l.Project_Custom_Name || ')', f.Project_Location) AS Location_Name
            FROM Finance_Ledger f
            LEFT JOIN Locations l ON f.Project_Location = l.Location_ID
        """
        if filter_type in ['CRK', 'DPC', 'JV']:
            base_query += " WHERE f.Voucher_Type = %s ORDER BY f.Record_Date DESC, f.Ledger_ID DESC"
            cur.execute(base_query, (filter_type,))
        else:
            base_query += " ORDER BY f.Record_Date DESC, f.Ledger_ID DESC"
            cur.execute(base_query)
            
        vouchers = cur.fetchall()
    except Exception as e:
        print("Finance Error:", e)
        vouchers = []
    finally:
        cur.close()
        conn.close()

    return render_template('finance.html', role=role, session=session, vouchers=vouchers, current_filter=filter_type)

@app.route('/add_income', methods=['GET', 'POST'])
def add_income():
    if 'user' not in session: return redirect('/login')
    if session.get('role') not in ['Admin', 'Finance']: return "Unauthorized", 403

    conn = get_db_connection()
    cur = conn.cursor()

    if request.method == 'POST':
        try:
            description = request.form['description']
            amount = float(request.form['amount'])
            location = request.form['location']
            receipt_type = request.form['receipt_type']
            
            # CRK-0001 ဖြင့် ထွက်မည် (ဂဏန်း ၄ လုံး)
            voucher_no = generate_serial("CRK", "Finance_Ledger", "Voucher_No", 4)
            
            if receipt_type == 'Cash':
                cur.execute("""
                    INSERT INTO Finance_Ledger (Voucher_Type, Voucher_No, Description, Account_Head, Dr_Amount, Cr_Amount, Project_Location)
                    VALUES ('CRK', %s, %s, 'Cash in Hand', %s, 0, %s),
                           ('CRK', %s, %s, 'Sales / Income', 0, %s, %s)
                """, (voucher_no, description, amount, location, voucher_no, description, amount, location))
            else:
                cur.execute("""
                    INSERT INTO Finance_Ledger (Voucher_Type, Voucher_No, Description, Account_Head, Dr_Amount, Cr_Amount, Project_Location)
                    VALUES ('CRK', %s, %s, 'Cash at Bank', %s, 0, %s),
                           ('CRK', %s, %s, 'Sales / Income', 0, %s, %s)
                """, (voucher_no, description, amount, location, voucher_no, description, amount, location))

            conn.commit()
            return redirect('/finance')
        except Exception as e:
            conn.rollback()
            return f"Error adding income: {str(e)}"
        finally:
            cur.close()
            conn.close()

    cur.execute("SELECT Location_ID, Base_Type, Project_Custom_Name FROM Locations WHERE Status = 'Active'")
    locations = cur.fetchall()
    cur.close()
    conn.close()
    return render_template('income_form.html', locations=locations)

@app.route('/add_expense', methods=['GET', 'POST'])
def add_expense():
    if 'user' not in session: return redirect('/login')
    if session.get('role') not in ['Admin', 'Finance']: return "Unauthorized", 403

    conn = get_db_connection()
    cur = conn.cursor()

    if request.method == 'POST':
        try:
            description = request.form['description']
            amount = float(request.form['amount'])
            location = request.form['location']
            expense_head = request.form['expense_head']
            
            # DPC-0001 ဖြင့် ထွက်မည် (ဂဏန်း ၄ လုံး)
            voucher_no = generate_serial("DPC", "Finance_Ledger", "Voucher_No", 4)
            
            cur.execute("""
                INSERT INTO Finance_Ledger (Voucher_Type, Voucher_No, Description, Account_Head, Dr_Amount, Cr_Amount, Project_Location)
                VALUES ('DPC', %s, %s, %s, %s, 0, %s),
                       ('DPC', %s, %s, 'Cash in Hand', 0, %s, %s)
            """, (voucher_no, description, expense_head, amount, location, voucher_no, description, amount, location))

            conn.commit()
            return redirect('/finance')
        except Exception as e:
            conn.rollback()
            return f"Error adding expense: {str(e)}"
        finally:
            cur.close()
            conn.close()

    cur.execute("SELECT Location_ID, Base_Type, Project_Custom_Name FROM Locations WHERE Status = 'Active'")
    locations = cur.fetchall()
    cur.close()
    conn.close()
    return render_template('expense_form.html', locations=locations)

@app.route('/delete_finance/<int:id>')
def delete_finance(id):
    if 'user' not in session: return redirect('/login')
    if session.get('role') != 'Admin': return "Unauthorized", 403

    conn = get_db_connection()
    cur = conn.cursor()
    try:
        cur.execute("DELETE FROM Finance_Ledger WHERE Ledger_ID = %s", (id,))
        conn.commit()
    except Exception as e:
        conn.rollback()
    finally:
        cur.close()
        conn.close()
    return redirect('/finance')

# ==========================================
# ၅။ GRN (ပစ္စည်းအဝင်) နှင့် GIN (ပစ္စည်းအထွက်) 
# ==========================================
@app.route('/add_grn', methods=['GET', 'POST'])
def add_grn():
    if 'user' not in session: return redirect('/login')
    
    conn = get_db_connection()
    cur = conn.cursor()

    if request.method == 'POST':
        try:
            po_id = request.form.get('po_id')
            item_name = request.form['item_name']
            qty = request.form['qty']
            amount = request.form['amount']
            payment_type = request.form['payment_type']
            location = request.form['location']
            
            # GRN-001 ဖြင့် ထွက်မည် (ဂဏန်း ၃ လုံး)
            form_no = generate_serial("GRN", "Inventory_Ledger", "Form_No", 3) 
            ref_no = f"PO-{po_id}" if po_id else "Direct"
            
            cur.execute("""
                INSERT INTO Inventory_Ledger (Form_Type, Form_No, Item_Code, Item_Name, Qty_In, To_Location)
                VALUES ('GRN', %s, %s, %s, %s, %s)
            """, (form_no, ref_no, item_name, qty, location))
            
            if payment_type == 'Cash':
                # Cash Payment ဆိုလျှင် DPC-0001
                voucher_no = generate_serial("DPC", "Finance_Ledger", "Voucher_No", 4)
                cur.execute("""
                    INSERT INTO Finance_Ledger (Voucher_Type, Voucher_No, Description, Account_Head, Dr_Amount, Cr_Amount, Project_Location)
                    VALUES ('DPC', %s, %s, 'Inventory Asset', %s, 0, %s),
                           ('DPC', %s, %s, 'Cash in Hand', 0, %s, %s)
                """, (voucher_no, f"{item_name} ဝယ်ယူမှု (GRN)", amount, location,
                      voucher_no, f"{item_name} ဝယ်ယူမှု (GRN)", amount, location))
            else:
                # Credit Payment ဆိုလျှင် JV-0001
                voucher_no = generate_serial("JV", "Finance_Ledger", "Voucher_No", 4)
                cur.execute("""
                    INSERT INTO Finance_Ledger (Voucher_Type, Voucher_No, Description, Account_Head, Dr_Amount, Cr_Amount, Project_Location)
                    VALUES ('JV', %s, %s, 'Inventory Asset', %s, 0, %s),
                           ('JV', %s, %s, 'Account Payable', 0, %s, %s)
                """, (voucher_no, f"{item_name} ဝယ်ယူမှု (GRN)", amount, location,
                      voucher_no, f"{item_name} ဝယ်ယူမှု (GRN)", amount, location))

            if po_id:
                cur.execute("UPDATE Purchase_Orders SET Status = 'Received' WHERE PO_ID = %s", (po_id,))

            conn.commit()
            return redirect('/inventory')
            
        except Exception as e:
            conn.rollback()
            return f"Error: {str(e)}"
        finally:
            cur.close()
            conn.close()

    cur.execute("SELECT * FROM Purchase_Orders WHERE Status = 'Approved'")
    approved_pos = cur.fetchall()
    cur.execute("SELECT Location_ID, Base_Type, Project_Custom_Name FROM Locations WHERE Status = 'Active'")
    locations = cur.fetchall()
    cur.close()
    conn.close()
    return render_template('grn_form.html', approved_pos=approved_pos, locations=locations)

@app.route('/requisition')
def requisition():
    if 'user' not in session: return redirect('/login')
    role = session.get('role')

    conn = get_db_connection()
    cur = conn.cursor()
    try:
        cur.execute("""
            SELECT 
                i.Ledger_ID, i.Record_Date, i.Form_Type, i.Form_No, 
                i.Item_Code, i.Item_Name, i.Qty_In, i.Qty_Out, 
                i.From_Location, i.To_Location,
                COALESCE(l.Base_Type || ' (' || l.Project_Custom_Name || ')', i.To_Location) AS Location_Name
            FROM Inventory_Ledger i
            LEFT JOIN Locations l ON i.To_Location = l.Location_ID
            WHERE i.Form_Type = 'GIN'
            ORDER BY i.Record_Date DESC, i.Ledger_ID DESC
        """)
        items = cur.fetchall()
    except Exception as e:
        items = []
    finally:
        cur.close()
        conn.close()

    return render_template('requisition.html', role=role, session=session, items=items)

@app.route('/add_gin', methods=['GET', 'POST'])
def add_gin():
    if 'user' not in session: return redirect('/login')
    conn = get_db_connection()
    cur = conn.cursor()

    if request.method == 'POST':
        try:
            item_name = request.form['item_name']
            qty = float(request.form['qty'])
            amount = float(request.form['amount'])
            location = request.form['location'] 
            
            cur.execute("SELECT COALESCE(SUM(Qty_In), 0) - COALESCE(SUM(Qty_Out), 0) FROM Inventory_Ledger WHERE Item_Name = %s", (item_name,))
            current_stock = cur.fetchone()[0]

            if current_stock is None or qty > current_stock:
                return f"<h2 style='color: red; text-align: center; margin-top: 50px;'>Error: {item_name} အတွက် ပစ္စည်းလက်ကျန် မလောက်ပါ။</h2>", 400

            # GIN-001 ဖြင့် ထွက်မည် (ဂဏန်း ၃ လုံး)
            form_no = generate_serial("GIN", "Inventory_Ledger", "Form_No", 3)
            
            cur.execute("""
                INSERT INTO Inventory_Ledger (Form_Type, Form_No, Item_Code, Item_Name, Qty_Out, To_Location)
                VALUES ('GIN', %s, 'ITEM-NEW', %s, %s, %s)
            """, (form_no, item_name, qty, location))
            
            # GIN အတွက် Journal Voucher ထွက်မည် JV-0001
            voucher_no = generate_serial("JV", "Finance_Ledger", "Voucher_No", 4)
            cur.execute("""
                INSERT INTO Finance_Ledger (Voucher_Type, Voucher_No, Description, Account_Head, Dr_Amount, Cr_Amount, Project_Location)
                VALUES ('JV', %s, %s, 'Site Expense (WIP)', %s, 0, %s),
                       ('JV', %s, %s, 'Inventory Asset', 0, %s, %s)
            """, (voucher_no, f"{item_name} ဆိုက်သို့ ထုတ်ပေးမှု", amount, location,
                  voucher_no, f"{item_name} ဆိုက်သို့ ထုတ်ပေးမှု", amount, location))

            conn.commit()
            return redirect('/requisition')
        except Exception as e:
            conn.rollback()
            return f"Error: {str(e)}"
        finally:
            cur.close()
            conn.close()

    cur.execute("SELECT Location_ID, Base_Type, Project_Custom_Name FROM Locations WHERE Status = 'Active'")
    locations = cur.fetchall()
    cur.close()
    conn.close()
    return render_template('gin_form.html', locations=locations)

# ==========================================
# ၆။ ဆိုက်အချင်းချင်း ပစ္စည်းလွှဲပြောင်းခြင်း (Transfer)
# ==========================================
@app.route('/transfer', methods=['GET'])
@app.route('/add_transfer', methods=['GET', 'POST'])
def add_transfer():
    if 'user' not in session: return redirect('/login')
    conn = get_db_connection()
    cur = conn.cursor()

    if request.method == 'POST':
        try:
            item_name = request.form['item_name']
            qty = float(request.form['qty'])
            amount = float(request.form['amount'])
            from_location = request.form['from_location']
            to_location = request.form['to_location']

            if from_location == to_location:
                return "<h3 style='color:red; text-align:center; margin-top:50px;'>Error: ပေးပို့မည့်ဆိုက်နှင့် လက်ခံမည့်ဆိုက် တူညီနေပါသည်။</h3>", 400

            # TRN-001 ဖြင့် ထွက်မည် (ဂဏန်း ၃ လုံး)
            form_no = generate_serial("TRN", "Inventory_Ledger", "Form_No", 3) 
            
            cur.execute("""
                INSERT INTO Inventory_Ledger (Form_Type, Form_No, Item_Code, Item_Name, Qty_Out, From_Location, To_Location)
                VALUES ('Transfer Out', %s, 'ITEM-NEW', %s, %s, %s, %s)
            """, (form_no, item_name, qty, from_location, to_location))
            
            cur.execute("""
                INSERT INTO Inventory_Ledger (Form_Type, Form_No, Item_Code, Item_Name, Qty_In, From_Location, To_Location)
                VALUES ('Transfer In', %s, 'ITEM-NEW', %s, %s, %s, %s)
            """, (form_no, item_name, qty, from_location, to_location))

            # JV-0001
            voucher_no = generate_serial("JV", "Finance_Ledger", "Voucher_No", 4)
            cur.execute("""
                INSERT INTO Finance_Ledger (Voucher_Type, Voucher_No, Description, Account_Head, Dr_Amount, Cr_Amount, Project_Location)
                VALUES ('JV', %s, %s, 'Site Expense (WIP)', 0, %s, %s),
                       ('JV', %s, %s, 'Site Expense (WIP)', %s, 0, %s)
            """, (voucher_no, f"{item_name} (Transfer to {to_location})", amount, from_location,
                  voucher_no, f"{item_name} (Transfer from {from_location})", amount, to_location))

            conn.commit()
            return redirect('/inventory')
        except Exception as e:
            conn.rollback()
            return f"Error: {str(e)}"
        finally:
            cur.close()
            conn.close()

    cur.execute("SELECT Location_ID, Base_Type, Project_Custom_Name FROM Locations WHERE Status = 'Active'")
    locations = cur.fetchall()
    cur.close()
    conn.close()
    return render_template('transfer_form.html', locations=locations)

# ==========================================
# ၇။ Location စီမံခန့်ခွဲခြင်း
# ==========================================
@app.route('/locations', methods=['GET', 'POST'])
def manage_locations():
    if 'user' not in session: return redirect('/login')
    role = session.get('role')
    if role != 'Admin': return "Unauthorized", 403

    conn = get_db_connection()
    cur = conn.cursor()

    if request.method == 'POST':
        try:
            for key, value in request.form.items():
                if key.startswith('loc_'):
                    loc_id = key.replace('loc_', '')
                    cur.execute("UPDATE Locations SET Project_Custom_Name = %s WHERE Location_ID = %s", (value, loc_id))
            conn.commit()
            return redirect('/locations')
        except Exception as e:
            conn.rollback()
        finally:
            cur.close()
            conn.close()

    cur.execute("SELECT Location_ID, Base_Type, Project_Custom_Name FROM Locations ORDER BY Location_ID")
    locations = cur.fetchall()
    cur.close()
    conn.close()
    return render_template('locations.html', role=role, session=session, locations=locations)

@app.route('/add_location', methods=['POST'])
def add_location():
    if session.get('role') != 'Admin': return "Unauthorized", 403
    conn = get_db_connection()
    cur = conn.cursor()
    try:
        loc_id = request.form['loc_id']
        base_type = request.form['base_type']
        custom_name = request.form['custom_name']
        cur.execute("INSERT INTO Locations (Location_ID, Base_Type, Project_Custom_Name) VALUES (%s, %s, %s)", (loc_id, base_type, custom_name))
        conn.commit()
    except Exception as e:
        conn.rollback()
    finally:
        cur.close()
        conn.close()
    return redirect('/locations')

# ==========================================
# ၈။ Report များ (Excel / PDF)
# ==========================================
@app.route('/export_finance_excel')
def export_finance_excel():
    if 'user' not in session or session.get('role') not in ['Admin', 'Finance']: return "Unauthorized", 403
    filter_type = request.args.get('filter')
    conn = get_db_connection()
    try:
        query = """
            SELECT f.Record_Date AS "နေ့စွဲ", f.Voucher_Type AS "ဘောက်ချာအမျိုးအစား", f.Voucher_No AS "ဘောက်ချာနံပါတ်", 
                   f.Description AS "အကြောင်းအရာ", f.Account_Head AS "ငွေစာရင်းခေါင်းစဉ်", f.Dr_Amount AS "ငွေထွက် (Dr)", 
                   f.Cr_Amount AS "ငွေဝင် (Cr)", COALESCE(l.Base_Type || ' (' || l.Project_Custom_Name || ')', f.Project_Location) AS "တည်နေရာ"
            FROM Finance_Ledger f LEFT JOIN Locations l ON f.Project_Location = l.Location_ID
        """
        params = None
        if filter_type in ['CRK', 'DPC', 'JV']:
            query += " WHERE f.Voucher_Type = %s"
            params = (filter_type,)
        query += " ORDER BY f.Record_Date ASC, f.Ledger_ID ASC"
        
        df = pd.read_sql_query(query, conn, params=params)
        output = BytesIO()
        with pd.ExcelWriter(output, engine='openpyxl') as writer:
            df.to_excel(writer, index=False, sheet_name='Finance_Report')
        output.seek(0)
        return send_file(output, download_name="Finance_Report.xlsx", as_attachment=True)
    finally:
        conn.close()

@app.route('/print_finance')
def print_finance():
    if 'user' not in session or session.get('role') not in ['Admin', 'Finance']: return "Unauthorized", 403
    filter_type = request.args.get('filter')
    conn = get_db_connection()
    cur = conn.cursor()
    try:
        query = """
            SELECT f.Record_Date, f.Voucher_Type, f.Voucher_No, f.Description, f.Account_Head, f.Dr_Amount, f.Cr_Amount, 
                   COALESCE(l.Base_Type || ' (' || l.Project_Custom_Name || ')', f.Project_Location) AS Location_Name
            FROM Finance_Ledger f LEFT JOIN Locations l ON f.Project_Location = l.Location_ID
        """
        params = []
        if filter_type in ['CRK', 'DPC', 'JV']:
            query += " WHERE f.Voucher_Type = %s"
            params.append(filter_type)
        query += " ORDER BY f.Record_Date ASC, f.Ledger_ID ASC"
        cur.execute(query, tuple(params))
        vouchers = cur.fetchall()
        total_dr = sum(v[5] for v in vouchers)
        total_cr = sum(v[6] for v in vouchers)
    except:
        vouchers = []; total_dr = total_cr = 0
    finally:
        cur.close()
        conn.close()
    return render_template('print_finance.html', vouchers=vouchers, total_dr=total_dr, total_cr=total_cr, current_filter=filter_type)

@app.route('/export_inventory_excel')
def export_inventory_excel():
    if 'user' not in session: return redirect('/login')
    conn = get_db_connection()
    try:
        query = """
            SELECT i.Record_Date AS "နေ့စွဲ", i.Form_Type AS "Form Type", i.Form_No AS "ဘောက်ချာ No.", i.Item_Name AS "ပစ္စည်းအမည်", 
                   i.Qty_In AS "အဝင် (Qty In)", i.Qty_Out AS "အထွက် (Qty Out)", COALESCE(l.Base_Type || ' (' || l.Project_Custom_Name || ')', i.To_Location) AS "တည်နေရာ"
            FROM Inventory_Ledger i LEFT JOIN Locations l ON i.To_Location = l.Location_ID ORDER BY i.Record_Date ASC, i.Form_No ASC
        """
        df = pd.read_sql_query(query, conn)
        output = BytesIO()
        with pd.ExcelWriter(output, engine='openpyxl') as writer:
            df.to_excel(writer, index=False, sheet_name='Inventory_Report')
        output.seek(0)
        return send_file(output, download_name="Inventory_Report.xlsx", as_attachment=True)
    finally:
        conn.close()

@app.route('/print_inventory')
def print_inventory():
    if 'user' not in session: return redirect('/login')
    conn = get_db_connection()
    cur = conn.cursor()
    try:
        cur.execute("""
            SELECT i.Record_Date, i.Form_Type, i.Form_No, i.Item_Name, i.Qty_In, i.Qty_Out, COALESCE(l.Base_Type || ' (' || l.Project_Custom_Name || ')', i.To_Location) 
            FROM Inventory_Ledger i LEFT JOIN Locations l ON i.To_Location = l.Location_ID ORDER BY i.Record_Date ASC, i.Form_No ASC
        """)
        items = cur.fetchall()
        total_in = sum(v[4] for v in items)
        total_out = sum(v[5] for v in items)
    except:
        items = []; total_in = total_out = 0
    finally:
        cur.close()
        conn.close()
    return render_template('print_inventory.html', items=items, total_in=total_in, total_out=total_out)

# ==========================================
# ၉။ Purchase Orders (PO) အပိုင်း
# ==========================================
@app.route('/po')
def view_po():
    if 'user' not in session: return redirect('/login')
    role = session.get('role')
    if role not in ['Admin', 'Purchaser', 'Store Keeper']: return "Unauthorized", 403

    conn = get_db_connection()
    cur = conn.cursor()
    try:
        if role in ['Admin', 'Store Keeper']:
            cur.execute("SELECT * FROM Purchase_Orders ORDER BY Record_Date DESC")
        else:
            cur.execute("SELECT * FROM Purchase_Orders WHERE Created_By = %s ORDER BY Record_Date DESC", (session['user'],))
        pos = cur.fetchall()
    finally:
        cur.close()
        conn.close()
    return render_template('po_list.html', role=role, session=session, pos=pos)

@app.route('/add_po', methods=['GET', 'POST'])
def add_po():
    if 'user' not in session: return redirect('/login')
    if session.get('role') not in ['Admin', 'Purchaser']: return "Unauthorized", 403

    conn = get_db_connection()
    cur = conn.cursor()

    if request.method == 'POST':
        item_name = request.form['item_name']
        qty = request.form['qty']
        amount = request.form['amount']
        supplier = request.form['supplier']
        target_location = request.form['location']
        po_no = "PO-" + datetime.now().strftime("%Y%m%d%H%M%S")
        created_by = session['user']
        
        try:
            cur.execute("INSERT INTO Purchase_Orders (PO_No, Item_Name, Qty, Estimated_Amount, Supplier_Name, Target_Location, Created_By) VALUES (%s, %s, %s, %s, %s, %s, %s)", (po_no, item_name, qty, amount, supplier, target_location, created_by))
            conn.commit()
            return redirect('/po')
        except:
            conn.rollback()
        finally:
            cur.close()
            conn.close()

    cur.execute("SELECT Location_ID, Base_Type, Project_Custom_Name FROM Locations WHERE Status = 'Active'")
    locations = cur.fetchall()
    cur.close()
    conn.close()
    return render_template('po_form.html', locations=locations)

@app.route('/approve_po/<int:po_id>')
def approve_po(po_id):
    if 'user' not in session or session.get('role') != 'Admin': return "Admin Only", 403
    conn = get_db_connection()
    cur = conn.cursor()
    try:
        cur.execute("UPDATE Purchase_Orders SET Status = 'Approved' WHERE PO_ID = %s", (po_id,))
        conn.commit()
    finally:
        cur.close()
        conn.close()
    return redirect('/po')

@app.route('/delete_po/<int:po_id>')
def delete_po(po_id):
    if 'user' not in session: return redirect('/login')
    role = session.get('role')
    user = session.get('user')
    conn = get_db_connection()
    cur = conn.cursor()
    try:
        if role == 'Admin':
            cur.execute("DELETE FROM Purchase_Orders WHERE PO_ID = %s", (po_id,))
        else:
            cur.execute("DELETE FROM Purchase_Orders WHERE PO_ID = %s AND Created_By = %s AND Status = 'Pending'", (po_id, user))
        conn.commit()
    finally:
        cur.close()
        conn.close()
    return redirect('/po')

@app.route('/export_po_excel')
def export_po_excel():
    if 'user' not in session: return redirect('/login')
    conn = get_db_connection()
    try:
        query = """
            SELECT Record_Date AS "နေ့စွဲ", PO_No AS "PO No.", Item_Name AS "ပစ္စည်းအမည်", Qty AS "အရေအတွက်", Estimated_Amount AS "ခန့်မှန်းတန်ဖိုး", 
                   Supplier_Name AS "Supplier", Created_By AS "တောင်းခံသူ", Status AS "Status"
            FROM Purchase_Orders ORDER BY Record_Date ASC
        """
        df = pd.read_sql_query(query, conn)
        output = BytesIO()
        with pd.ExcelWriter(output, engine='openpyxl') as writer:
            df.to_excel(writer, index=False, sheet_name='PO_Report')
        output.seek(0)
        return send_file(output, download_name="PO_Report.xlsx", as_attachment=True)
    finally:
        conn.close()

@app.route('/print_po')
def print_po():
    if 'user' not in session: return redirect('/login')
    conn = get_db_connection()
    cur = conn.cursor()
    try:
        cur.execute("SELECT Record_Date, PO_No, Item_Name, Qty, Estimated_Amount, Supplier_Name, Created_By, Status FROM Purchase_Orders ORDER BY Record_Date ASC")
        pos = cur.fetchall()
    finally:
        cur.close()
        conn.close()
    return render_template('print_po.html', pos=pos)

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=True)