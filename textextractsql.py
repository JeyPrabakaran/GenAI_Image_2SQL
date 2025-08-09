from dotenv import load_dotenv
import streamlit as st
import os
import google.generativeai as genai
from PIL import Image
from datetime import datetime
import json
import pyodbc
import re
import base64
import hashlib
import time
import fitz  # PyMuPDF for PDF processing
import io
from typing import List
import pandas as pd

# App UI Configuration
st.set_page_config(page_title="🧾 Multi-Page Invoice Extractor", layout='wide')

# Inject CSS from external file
def local_css(file_name):
    try:
        with open(file_name) as f:
            st.markdown(f"<style>{f.read()}</style>", unsafe_allow_html=True)
    except FileNotFoundError:
        st.warning("CSS file not found. Using default styling.")

local_css("style.css")

# Function to convert local image to base64
def set_background_image(image_file_path):
    try:
        with open(image_file_path, "rb") as img_file:
            encoded = base64.b64encode(img_file.read()).decode()
        css = f"""
        <style>
        .stApp {{
            background-image: url("data:image/png;base64,{encoded}");
            background-size: cover;
            background-position: center;
            background-repeat: no-repeat;
            background-attachment: fixed;
        }}
        </style>
        """
        st.markdown(css, unsafe_allow_html=True)
    except FileNotFoundError:
        st.warning("Background image not found.")

# Call this with your image file path
set_background_image("assets/bg1.webp")

# Load environment variables
load_dotenv()
genai.configure(api_key=os.getenv('GOOGLE_API_KEY'))

# Initialize Gemini model
model = genai.GenerativeModel("gemini-2.5-flash")

# Initialize session state
if 'authenticated' not in st.session_state:
    st.session_state.authenticated = False
if 'username' not in st.session_state:
    st.session_state.username = None
if 'raw_json' not in st.session_state:
    st.session_state.raw_json = ""
if 'current_images' not in st.session_state:
    st.session_state.current_images = []

# Hash password function
def hash_password(password):
    return hashlib.sha256(password.encode()).hexdigest()

# Connect to SQL Server
def get_sql_server_connection():
    conn = pyodbc.connect(
        "Driver={ODBC Driver 17 for SQL Server};"
        "Server=DESKTOP-LM2ET8D\SQLEXPRESS;"   # Change to your SQL Server name
        "Database=DEMODB1;"             # Change to your database
        "Trusted_Connection=yes;"         # Or use UID and PWD
    )
    return conn

# PDF to images conversion
def pdf_to_images(pdf_file) -> List[Image.Image]:
    """Convert PDF pages to PIL Images"""
    try:
        # Read PDF file
        pdf_document = fitz.open(stream=pdf_file.read(), filetype="pdf")
        images = []
        
        for page_num in range(len(pdf_document)):
            page = pdf_document.load_page(page_num)
            # Convert page to image (300 DPI for good quality)
            pix = page.get_pixmap(matrix=fitz.Matrix(300/72, 300/72))
            img_data = pix.tobytes("png")
            img = Image.open(io.BytesIO(img_data))
            images.append(img)
        
        pdf_document.close()
        return images
    except Exception as e:
        st.error(f"Error processing PDF: {e}")
        return []

# Process multiple images function
def process_multiple_images(uploaded_files) -> List[Image.Image]:
    """Process multiple uploaded files (images or PDFs)"""
    all_images = []
    
    for uploaded_file in uploaded_files:
        file_type = uploaded_file.type
        
        if file_type == "application/pdf":
            # Process PDF
            pdf_images = pdf_to_images(uploaded_file)
            all_images.extend(pdf_images)
        elif file_type in ["image/jpeg", "image/jpg", "image/png"]:
            # Process image
            image = Image.open(uploaded_file)
            all_images.append(image)
        else:
            st.warning(f"Unsupported file type: {file_type}")
    
    return all_images

# Database setup functions
def setup_database():
    """Create necessary tables if they don't exist"""
    try:
        conn = get_sql_server_connection()
        cursor = conn.cursor()
        
        # Create Users table
        cursor.execute("""
            IF NOT EXISTS (SELECT * FROM sysobjects WHERE name='Users' AND xtype='U')
            CREATE TABLE Users (
                id INT IDENTITY(1,1) PRIMARY KEY,
                username NVARCHAR(50) UNIQUE NOT NULL,
                password_hash NVARCHAR(64) NOT NULL,
                created_date DATETIME DEFAULT GETDATE(),
                is_admin BIT DEFAULT 0
            )
        """)
        
        # Create AuditLog table
        cursor.execute("""
            IF NOT EXISTS (SELECT * FROM sysobjects WHERE name='AuditLog' AND xtype='U')
            CREATE TABLE AuditLog (
                id INT IDENTITY(1,1) PRIMARY KEY,
                username NVARCHAR(50) NOT NULL,
                action NVARCHAR(100) NOT NULL,
                details NVARCHAR(MAX),
                timestamp DATETIME DEFAULT GETDATE()
            )
        """)
        
        # Create InvoiceMaster table if it doesn't exist
        cursor.execute("""
            IF NOT EXISTS (SELECT * FROM sysobjects WHERE name='InvoiceMaster' AND xtype='U')
            CREATE TABLE InvoiceMaster (
                id INT IDENTITY(1,1) NOT NULL,
                invoice_id NVARCHAR(50) PRIMARY KEY,
                customer NVARCHAR(100),
                invoice_date DATE,
                total DECIMAL(10,2),
                created_by NVARCHAR(50),
                created_date DATETIME DEFAULT GETDATE()
            )
        """)
        
        # Create InvoiceItems table if it doesn't exist
        cursor.execute("""
            IF NOT EXISTS (SELECT * FROM sysobjects WHERE name='InvoiceItems' AND xtype='U')
            CREATE TABLE InvoiceItems (
                id INT IDENTITY(1,1) PRIMARY KEY,
                invoice_id NVARCHAR(50) NOT NULL,
                description NVARCHAR(200),
                quantity INT,
                price DECIMAL(10,2)
            )
        """)
        
        # Create default admin user if no users exist
        cursor.execute("SELECT COUNT(*) FROM Users")
        user_count = cursor.fetchone()[0]
        if user_count == 0:
            admin_password_hash = hash_password("admin123")
            cursor.execute("""
                INSERT INTO Users (username, password_hash, is_admin)
                VALUES (?, ?, 1)
            """, "admin", admin_password_hash)
        
        conn.commit()
        conn.close()
    except Exception as e:
        st.error(f"Database setup error: {e}")

# Authentication functions
def verify_user(username, password):
    try:
        conn = get_sql_server_connection()
        cursor = conn.cursor()
        password_hash = hash_password(password)
        
        cursor.execute("""
            SELECT username, is_admin FROM Users 
            WHERE username = ? AND password_hash = ?
        """, username, password_hash)
        
        result = cursor.fetchone()
        conn.close()
        return result
    except Exception as e:
        st.error(f"Authentication error: {e}")
        return None

def add_user(username, password, is_admin=False):
    try:
        conn = get_sql_server_connection()
        cursor = conn.cursor()
        password_hash = hash_password(password)
        
        cursor.execute("""
            INSERT INTO Users (username, password_hash, is_admin)
            VALUES (?, ?, ?)
        """, username, password_hash, is_admin)
        
        conn.commit()
        conn.close()
        return True
    except Exception as e:
        st.error(f"Error adding user: {e}")
        return False

def log_audit(username, action, details=""):
    try:
        conn = get_sql_server_connection()
        cursor = conn.cursor()
        
        cursor.execute("""
            INSERT INTO AuditLog (username, action, details)
            VALUES (?, ?, ?)
        """, username, action, details)
        
        conn.commit()
        conn.close()
    except Exception as e:
        st.error(f"Audit logging error: {e}")

def get_audit_logs(limit=50):
    try:
        conn = get_sql_server_connection()
        cursor = conn.cursor()
        
        cursor.execute("""
            SELECT TOP (?) username, action, details, timestamp 
            FROM AuditLog 
            ORDER BY timestamp DESC
        """, limit)
        
        results = cursor.fetchall()
        conn.close()
        return results
    except Exception as e:
        st.error(f"Error fetching audit logs: {e}")
        return []

# Invoice history functions
def search_invoices(search_term="", search_type="all"):
    try:
        conn = get_sql_server_connection()
        cursor = conn.cursor()
        
        if search_type == "invoice_id":
            query = """
                SELECT im.invoice_id, im.customer, im.invoice_date, im.total, im.created_by, im.created_date
                FROM InvoiceMaster im
                WHERE im.invoice_id LIKE ?
                ORDER BY im.created_date DESC
            """
            cursor.execute(query, f"%{search_term}%")
        elif search_type == "customer":
            query = """
                SELECT im.invoice_id, im.customer, im.invoice_date, im.total, im.created_by, im.created_date
                FROM InvoiceMaster im
                WHERE im.customer LIKE ?
                ORDER BY im.created_date DESC
            """
            cursor.execute(query, f"%{search_term}%")
        else:  # all
            query = """
                SELECT im.invoice_id, im.customer, im.invoice_date, im.total, im.created_by, im.created_date
                FROM InvoiceMaster im
                ORDER BY im.created_date DESC
            """
            cursor.execute(query)
        
        results = cursor.fetchall()
        conn.close()
        return results
    except Exception as e:
        st.error(f"Error searching invoices: {e}")
        return []

def get_invoice_details(invoice_id):
    try:
        conn = get_sql_server_connection()
        cursor = conn.cursor()
        
        # Get master data
        cursor.execute("""
            SELECT invoice_id, customer, invoice_date, total, created_by, created_date
            FROM InvoiceMaster
            WHERE invoice_id = ?
        """, invoice_id)
        master_data = cursor.fetchone()
        
        # Get items data
        cursor.execute("""
            SELECT description, quantity, price
            FROM InvoiceItems
            WHERE invoice_id = ?
        """, invoice_id)
        items_data = cursor.fetchall()
        
        conn.close()
        return master_data, items_data
    except Exception as e:
        st.error(f"Error fetching invoice details: {e}")
        return None, None

# Natural Language Query Functions
def convert_query_to_sql(user_query):
    """Convert natural language query to SQL using Gemini"""
    prompt = f'''
    You are an expert in converting English queries to SQL! 
    The database has the table InvoiceMaster with columns like customer, invoice_id, invoice_date, total, created_by, created_date.
    The database has the table InvoiceItems with columns like id, invoice_id, description, quantity, price.
    
    Example 1: How many records are there in the table?
    SQL: SELECT COUNT(*) FROM InvoiceMaster;
    
    Example 2: List all customers.
    SQL: SELECT customer FROM InvoiceMaster;
    
    Example 3: Show me all invoices for customer John
    SQL: SELECT * FROM InvoiceMaster WHERE customer LIKE '%John%';
    
    Example 4: What items are in invoice INV-001?
    SQL: SELECT * FROM InvoiceItems WHERE invoice_id = 'INV-001';
    
    Example 5: Show total sales by customer
    SQL: SELECT customer, SUM(total) as total_sales FROM InvoiceMaster GROUP BY customer;
    
    Only return the SQL query. Do not include markdown or explanations.
    
    User Query: {user_query}
    '''
    
    try:
        response = model.generate_content(prompt)
        sql_query = response.text.strip()
        # Clean up the response - remove any markdown formatting
        sql_query = re.sub(r'```sql\n?', '', sql_query)
        sql_query = re.sub(r'```\n?', '', sql_query)
        return sql_query
    except Exception as e:
        st.error(f"Error converting query to SQL: {e}")
        return None

def execute_sql_query(sql_query):
    """Execute SQL query and return results"""
    try:
        conn = get_sql_server_connection()
        cursor = conn.cursor()
        
        cursor.execute(sql_query)
        
        # Get column names
        columns = [desc[0] for desc in cursor.description]
        
        # Get results
        results = cursor.fetchall()
        
        conn.close()
        
        # Convert to DataFrame for better display
        if results:
            df = pd.DataFrame.from_records(results, columns=columns)
            return df, None
        else:
            return None, "No results found."
            
    except Exception as e:
        return None, f"Error executing query: {e}"

# Natural Language Query Interface
def show_query_interface():
    st.title("Natural Language Query Interface")
    
    st.markdown("""
    **Ask questions about your invoices in plain English!**
    
    Examples:
    - "How many invoices do we have?"
    - "List all customers"
    - "Show me invoices from John Doe"
    - "What are the total sales by customer?"
    - "Show items in invoice INV-001"
    """)
    
    # Query input
    user_query = st.text_input(
        "Ask your question:",
        placeholder="e.g., How many invoices do we have this month?"
    )
    
    col1, col2 = st.columns([1, 4])
    
    with col1:
        query_button = st.button("🔍 Query", type="primary")
    
    if query_button and user_query:
        with st.spinner("Converting your query to SQL..."):
            sql_query = convert_query_to_sql(user_query)
            
        if sql_query:
            st.subheader("Generated SQL Query:")
            st.code(sql_query, language="sql")
            
            # Execute the query
            with st.spinner("Executing query..."):
                results_df, error = execute_sql_query(sql_query)
            
            if error:
                st.error(f"❌ {error}")
            elif results_df is not None:
                st.subheader("Query Results:")
                st.dataframe(results_df, use_container_width=True)
                
                # Log the query
                log_audit(st.session_state.username, "Natural language query executed", f"Query: {user_query}")
            else:
                st.info("No results found for your query.")

# Setup database on app start
setup_database()

# Login page
def show_login_page():
    st.title("🔐 Login to Invoice Extractor")
    
    col1, col2, col3 = st.columns([1, 2, 1])
    
    with col2:
        st.markdown("### Please login to continue")
        username = st.text_input("Username", key="login_username")
        password = st.text_input("Password", type="password", key="login_password")
        
        col_login, col_space = st.columns([1, 1])
        with col_login:
            if st.button("Login", use_container_width=True):
                if username and password:
                    user_result = verify_user(username, password)
                    if user_result:
                        st.session_state.authenticated = True
                        st.session_state.username = user_result[0]
                        st.session_state.is_admin = user_result[1]
                        log_audit(username, "User logged in")
                        st.success("Login successful!")
                        st.rerun()
                    else:
                        st.error("Invalid username or password!")
                        log_audit(username, "Failed login attempt")
                else:
                    st.warning("Please enter both username and password!")

# User management page
def show_user_management():
    st.title("👥 User Management")
    
    if not st.session_state.get('is_admin', False):
        st.error("Access denied. Admin privileges required.")
        return
    
    st.markdown('<div class="custom-tab-container">', unsafe_allow_html=True)
    tab1, tab2, tab3 = st.tabs(["Add User", "User List", "Audit Logs"])
    st.markdown('</div>', unsafe_allow_html=True)
    
    with tab1:
        st.subheader("Add New User")
        new_username = st.text_input("Username", key="new_username")
        new_password = st.text_input("Password", type="password", key="new_password")
        is_admin = st.checkbox("Admin privileges")
        
        if st.button("Add User"):
            if new_username and new_password:
                if add_user(new_username, new_password, is_admin):
                    st.success(f"User '{new_username}' added successfully!")
                    log_audit(st.session_state.username, f"Added new user: {new_username}")
                    time.sleep(1)
                    st.rerun()
            else:
                st.warning("Please enter both username and password!")
    
    with tab2:
        st.subheader("Existing Users")
        try:
            conn = get_sql_server_connection()
            cursor = conn.cursor()
            cursor.execute("SELECT username, created_date, is_admin FROM Users ORDER BY created_date DESC")
            users = cursor.fetchall()
            conn.close()
            
            if users:
                for user in users:
                    col1, col2, col3 = st.columns([2, 2, 1])
                    with col1:
                        st.write(f"**{user[0]}**")
                    with col2:
                        st.write(f"Created: {user[1].strftime('%Y-%m-%d')}")
                    with col3:
                        if user[2]:
                            st.write("🔒 Admin")
                        else:
                            st.write("👤 User")
            else:
                st.info("No users found.")
        except Exception as e:
            st.error(f"Error fetching users: {e}")
    
    with tab3:
        st.subheader("Audit Logs")
        logs = get_audit_logs()
        if logs:
            for log in logs:
                with st.expander(f"{log[1]} by {log[0]} - {log[3].strftime('%Y-%m-%d %H:%M:%S')}"):
                    st.write(f"**Action:** {log[1]}")
                    st.write(f"**User:** {log[0]}")
                    st.write(f"**Details:** {log[2]}")
                    st.write(f"**Timestamp:** {log[3]}")
        else:
            st.info("No audit logs found.")

# Invoice history page
def show_invoice_history():
    st.title("📊 Invoice History")
    
    # Search section
    col1, col2, col3 = st.columns([2, 1, 1])
    
    with col1:
        search_term = st.text_input("🔍 Search invoices", placeholder="Enter invoice ID or customer name...")
    
    with col2:
        search_type = st.selectbox("Search by", ["all", "invoice_id", "customer"])
    
    with col3:
        search_button = st.button("Search", use_container_width=True)
    
    # Get search results
    if search_button or search_term:
        invoices = search_invoices(search_term, search_type)
    else:
        invoices = search_invoices()  # Get all invoices
    
    # Display results
    if invoices:
        st.markdown(f"**Found {len(invoices)} invoice(s)**")
        
        for invoice in invoices:
            with st.expander(f"📄 {invoice[0]} - {invoice[1]} (${invoice[3]:.2f})"):
                col1, col2 = st.columns(2)
                
                with col1:
                    st.write(f"**Invoice ID:** {invoice[0]}")
                    st.write(f"**Customer:** {invoice[1]}")
                    st.write(f"**Invoice Date:** {invoice[2]}")
                    st.write(f"**Total:** ${invoice[3]:.2f}")
                
                with col2:
                    st.write(f"**Created By:** {invoice[4]}")
                    st.write(f"**Created Date:** {invoice[5].strftime('%Y-%m-%d %H:%M:%S')}")
                
                # Show invoice details
                if st.button(f"View Details", key=f"details_{invoice[0]}"):
                    master_data, items_data = get_invoice_details(invoice[0])
                    if master_data and items_data:
                        st.subheader("Invoice Items:")
                        for item in items_data:
                            st.write(f"• {item[0]} - Qty: {item[1]}, Price: ${item[2]:.2f}")
    else:
        st.info("No invoices found.")

# Main invoice extraction page
def show_invoice_page():
    # Header with logout button
    header_col1, header_col2 = st.columns([0.95, 0.05])
    with header_col1:
        st.title("💡Invoice Extractor: PDF & Images to SQL using Gemini")
    with header_col2:
        st.write(f"Welcome, **{st.session_state.username}**")
        if st.button("Logout"):
            log_audit(st.session_state.username, "User logged out")
            st.session_state.authenticated = False
            st.session_state.username = None
            st.session_state.is_admin = False
            st.rerun()

    # Navigation menu
    menu = ["Invoice Extraction", "Invoice History", "Query Interface"]
    if st.session_state.get('is_admin', False):
        menu.append("User Management")
    
    selected_page = st.selectbox("Navigation", menu)
    
    if selected_page == "User Management":
        show_user_management()
        return
    elif selected_page == "Invoice History":
        show_invoice_history()
        return
    elif selected_page == "Query Interface":
        show_query_interface()
        return
    
    # Layout in two columns
    left_col, right_col = st.columns(2)

    with right_col:
        uploaded_files = st.file_uploader(
            "📤 Upload Invoice Files (Images or PDF)", 
            type=["jpg", "jpeg", "png", "pdf"],
            accept_multiple_files=True
        )
        
        if uploaded_files:
            # Process all uploaded files
            all_images = process_multiple_images(uploaded_files)
            st.session_state.current_images = all_images
            
            if all_images:
                st.write(f"📊 Total pages/images: {len(all_images)}")
                
                # Show thumbnails of all pages
                cols = st.columns(min(3, len(all_images)))
                for idx, img in enumerate(all_images):
                    with cols[idx % 3]:
                        st.image(img, caption=f"Page {idx + 1}", use_column_width=True)

    with left_col:
        user_prompt = st.text_area(
            '✍️ Enter your custom prompt:',
            height=100,
            key="prompt_input",
            placeholder='Optional: Add specific instructions for extraction...'
        )
        
        # Add button and store its state
        extract_button = st.button("🔍 Extract Invoice Data")

    # System prompt for multi-page processing
    system_prompt = """
    You are a professional invoice extractor designed to handle multi-page invoices.
    Read ALL the uploaded images carefully and consolidate the information from ALL pages into 
    **ONE** comprehensive invoice structure in **strict JSON format**.

    Important instructions:
    1. Combine information from all pages into a single invoice
    2. Aggregate all line items from all pages
    3. Use the total amount from the final page or summary page
    4. Return **ONLY** the extracted structured information in **strict JSON format**, no extra text or explanation

    Your response must look like this (with sample values):

    {
      "invoice_id": "INV-001",
      "customer": "John Doe",
      "invoice_date": "2024-06-15",
      "total": 250.75,
      "items": [
        {
          "description": "Product A",
          "quantity": 2,
          "price": 100.00
        },
        {
          "description": "Service Fee",
          "quantity": 1,
          "price": 50.75
        }
      ]
    }

    Notes:
    - Return only valid, strict JSON
    - Use double quotes for all keys and string values
    - No trailing commas
    - No extra formatting or Markdown
    - All values must be filled or null (avoid empty strings)
    - Consolidate ALL items from ALL pages
    """

    # Function to prepare multiple images for Gemini
    def prepare_image_data_list(images: List[Image.Image]):
        image_parts = []
        for img in images:
            # Convert PIL Image to bytes
            img_byte_arr = io.BytesIO()
            img.save(img_byte_arr, format='PNG')
            img_byte_arr = img_byte_arr.getvalue()
            
            image_parts.append({
                'mime_type': 'image/png',
                'data': img_byte_arr
            })
        return image_parts

    # Function to get Gemini response for multiple images
    def get_gemini_response_multi(prompt_input, image_data_list, system_prompt):
        # Create the content list with all images
        content = [prompt_input, system_prompt] + image_data_list
        response = model.generate_content(content)
        return response.text

    def clean_json_response(response):
        try:
            # Remove everything before the first '{' and after the last '}'
            cleaned = re.search(r'{.*}', response, re.DOTALL).group()
            return json.loads(cleaned)
        except Exception as e:
            st.error(f"❌ Couldn't extract valid JSON from response: {e}")
            return None

    # Function to insert invoice data into SQL Server
    def insert_invoice_data_to_sql_server(data):
        try:
            conn = get_sql_server_connection()
            cursor = conn.cursor()

            cursor.execute("""
                INSERT INTO InvoiceMaster (invoice_id, customer, invoice_date, total, created_by)
                VALUES (?, ?, ?, ?, ?)
            """, data['invoice_id'], data['customer'], data['invoice_date'], data['total'], st.session_state.username)

            for item in data['items']:
                cursor.execute("""
                    INSERT INTO InvoiceItems (invoice_id, description, quantity, price)
                    VALUES (?, ?, ?, ?)
                """, data['invoice_id'], item['description'], item['quantity'], item['price'])

            conn.commit()
            conn.close()
            
            # Log the action
            log_audit(st.session_state.username, f"Inserted  invoice data", f"Invoice ID: {data['invoice_id']}")
            
            st.markdown('<div class="custom-success">✅ Invoice data inserted successfully into SQL Server!</div>', unsafe_allow_html=True)
            return True
        except Exception as e:
            st.error(f"❌ Error while inserting data: {e}")
            return False

    # Show response immediately below button
    if extract_button:
        if st.session_state.current_images:
            try:
                with st.spinner("🔄 Processing all pages..."):
                    image_data_list = prepare_image_data_list(st.session_state.current_images)
                    response = get_gemini_response_multi(user_prompt, image_data_list, system_prompt)
                    st.session_state.raw_json = response
                
                # Log extraction attempt
                log_audit(st.session_state.username, f"Extracted multi-page invoice data", f"Pages processed: {len(st.session_state.current_images)}")
                
            except Exception as e:
                st.error(f"Error during extraction: {e}")
        else:
            st.warning("Please upload invoice files first!")

    # Show editable JSON if available (only for admins)
    if st.session_state.raw_json:
        with left_col:
            # Only show editable JSON for admins
            if st.session_state.get('is_admin', False):
                st.subheader("📝 Raw JSON Data (Editable - Admin Only):")
                edited_json = st.text_area(
                    "Edit the extracted JSON data if needed:",
                    value=st.session_state.raw_json,
                    height=200,
                    key="editable_json"
                )
            else:
                # For normal users, just parse the JSON without showing the text area
                edited_json = st.session_state.raw_json
            
            # Parse and validate JSON
            try:
                data = clean_json_response(edited_json)
                if data:
                    st.success("✅ JSON is valid!")
                    
                    # Show preview
                    with st.expander("📋 Data Preview"):
                        st.json(data)
                    
                    # Confirmation dialog
                    st.subheader("💾 Save to Database")
                    col1, col2 = st.columns(2)
                    
                    with col1:
                        if st.button("💾 Insert into SQL Database", type="primary"):
                            # Show confirmation dialog
                            st.session_state.show_confirmation = True
                    
                    with col2:
                        if st.button("🗑️ Clear Data"):
                            st.session_state.raw_json = ""
                            st.session_state.current_images = []
                            st.rerun()
                    
                    # Confirmation dialog
                    if st.session_state.get('show_confirmation', False):
                        st.markdown('<div class="custom-success">⚠️ Are you sure you want to insert this data into the database?</div>', unsafe_allow_html=True)
                        #st.warning("⚠️ Are you sure you want to insert this data into the database?")
                        
                        col1, col2, col3 = st.columns(3)
                        with col1:
                            if st.button("✅ Yes, Insert", type="primary"):
                                if insert_invoice_data_to_sql_server(data):
                                    st.session_state.raw_json = ""
                                    st.session_state.current_images = []
                                    st.session_state.show_confirmation = False
                                    time.sleep(2)
                                    st.rerun()
                        
                        with col2:
                            if st.button("❌ Cancel"):
                                st.session_state.show_confirmation = False
                                st.rerun()
                        
                        with col3:
                            st.empty()  # Spacer
                            
            except Exception as e:
                st.error(f"❌ Invalid JSON: {e}")

# Main app logic
def main():
    if not st.session_state.authenticated:
        show_login_page()
    else:
        show_invoice_page()

if __name__ == "__main__":
    main()