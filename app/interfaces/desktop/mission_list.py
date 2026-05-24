import os
from PyQt6.QtWidgets import (QDialog, QVBoxLayout, QHBoxLayout, QLabel, 
                             QPushButton, QScrollArea, QWidget, QFrame)
from PyQt6.QtCore import Qt
from app.core.logger import log
from app.contracts.mission import Mission
from app.interfaces.desktop.mission_ui_status import mission_ui_detail_status_line

class NevlanDesign:
    BG = "rgba(25, 25, 32, 250)"
    BORDER = "rgba(255, 255, 255, 35)"
    BTN_BG = "rgba(255, 255, 255, 20)"
    BTN_HOVER = "rgba(255, 255, 255, 40)"

class MissionListDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint | Qt.WindowType.Dialog)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.resize(350, 450)
        
        try:
            from PyQt6.QtGui import QGuiApplication
            screen_geo = QGuiApplication.primaryScreen().geometry()
            x = (screen_geo.width() - self.width()) // 2
            y = (screen_geo.height() - self.height()) // 2
            self.move(x, y)
        except Exception as e:
            pass
            
        self.layout = QVBoxLayout(self)
        self.layout.setContentsMargins(0, 0, 0, 0)
        
        self.container = QFrame()
        self.container.setStyleSheet(f"""
            QFrame {{
                background-color: {NevlanDesign.BG};
                border: 1px solid {NevlanDesign.BORDER};
                border-radius: 12px;
            }}
        """)
        self.container_layout = QVBoxLayout(self.container)
        self.container_layout.setContentsMargins(20, 20, 20, 20)
        self.layout.addWidget(self.container)
        
        # Header
        header_layout = QHBoxLayout()
        title_lbl = QLabel("Automatizaciones")
        title_lbl.setStyleSheet("color: white; font-size: 16px; font-weight: bold; border: none; background: transparent;")
        
        close_btn = QPushButton("✖")
        close_btn.setStyleSheet(f"""
            QPushButton {{ background: transparent; color: white; border: none; font-size: 16px; }}
            QPushButton:hover {{ color: #ff7675; }}
        """)
        close_btn.clicked.connect(self.reject)
        close_btn.setFixedSize(30, 30)
        
        header_layout.addWidget(title_lbl)
        header_layout.addStretch()
        header_layout.addWidget(close_btn)
        self.container_layout.addLayout(header_layout)
        
        # Scroll Area
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setStyleSheet("QScrollArea { border: none; background: transparent; }")
        
        self.list_widget = QWidget()
        self.list_widget.setStyleSheet("background: transparent;")
        self.list_layout = QVBoxLayout(self.list_widget)
        self.list_layout.setContentsMargins(0, 0, 0, 0)
        self.list_layout.setSpacing(10)
        
        self.scroll.setWidget(self.list_widget)
        self.container_layout.addWidget(self.scroll)
        
        self._load_missions()
        
    def _load_missions(self):
        from app.services.missions import mission_store
        
        missions = mission_store.list_all()
        # Sort by updated_at descending if available
        try:
            missions.sort(key=lambda x: x.updated_at, reverse=True)
        except:
            pass
        
        if not missions:
            empty_lbl = QLabel("No hay automatizaciones guardadas.")
            empty_lbl.setStyleSheet("color: #aaa; background: transparent; border: none;")
            empty_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self.list_layout.addWidget(empty_lbl)
            
        for mission in missions:
            btn = QPushButton(f"⚡ {mission.name}")
            btn.setToolTip(mission_ui_detail_status_line(mission))
            btn.setStyleSheet(f"""
                QPushButton {{
                    background-color: {NevlanDesign.BTN_BG};
                    color: white;
                    border: 1px solid {NevlanDesign.BORDER};
                    border-radius: 6px;
                    padding: 12px;
                    text-align: left;
                    font-size: 14px;
                }}
                QPushButton:hover {{ background-color: {NevlanDesign.BTN_HOVER}; }}
            """)
            btn.clicked.connect(lambda checked, m=mission: self.open_mission(m))
            self.list_layout.addWidget(btn)
            
        self.list_layout.addStretch()

    def open_mission(self, mission):
        from app.interfaces.desktop.mission_review import MissionReviewDialog
        dialog = MissionReviewDialog(mission, self.parent())
        self.accept()
        dialog.exec()
