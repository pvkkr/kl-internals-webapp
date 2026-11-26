import json
import unittest
from unittest.mock import MagicMock

from bs4 import BeautifulSoup

import app as webapp


class TestKLInternalsParsers(unittest.TestCase):
    def test_extract_ajax_html_json_and_raw(self):
        mock_json = MagicMock()
        mock_json.text = json.dumps({"title": "Handout", "content": "<table><tr><th>Evaluation Component</th></tr></table>"})
        mock_json.json.return_value = {"title": "Handout", "content": "<table><tr><th>Evaluation Component</th></tr></table>"}
        soup1 = webapp.extract_ajax_html(mock_json)
        self.assertIsNotNone(soup1.find("table"))

        mock_raw = MagicMock()
        mock_raw.text = "<div><table><tr><td>OK</td></tr></table></div>"
        soup2 = webapp.extract_ajax_html(mock_raw)
        self.assertIsNotNone(soup2.find("table"))

    def test_parse_evaluation_plan_with_title_row_and_rowspan(self):
        html = """
        <table>
          <tr><th colspan="5">EVALUATION PLAN</th></tr>
          <tr>
            <th>Evaluation Type</th>
            <th>Evaluation Component</th>
            <th>Metric</th>
            <th>Value</th>
          </tr>
          <tr>
            <td rowspan="2">End Semester Summative Evaluation (Total = 40%)</td>
            <td rowspan="2">End Semester Exam</td>
            <td>Weightage</td><td>24</td>
          </tr>
          <tr><td>Max Marks</td><td>100</td></tr>
          <tr>
            <td rowspan="4">In Semester Formative Evaluation (Total = 24%)</td>
            <td rowspan="2">ALM</td>
            <td>Weightage</td><td>8</td>
          </tr>
          <tr><td>Max Marks</td><td>40</td></tr>
          <tr>
            <td rowspan="2">Home Assignment and Textbook</td>
            <td>Weightage</td><td>6</td>
          </tr>
          <tr><td>Max Marks</td><td>40</td></tr>
          <tr>
            <td rowspan="2">In-Semester Summative Evaluation (Total = 36%)</td>
            <td rowspan="2">Semester in Exam-I</td>
            <td>Weightage</td><td>14</td>
          </tr>
          <tr><td>Max Marks</td><td>50</td></tr>
        </table>
        """
        plan = webapp.parse_evaluation_plan(BeautifulSoup(html, "html.parser"))
        by_name = {c["name"]: c for c in plan}
        self.assertNotIn("End Semester Exam", by_name)
        self.assertEqual(by_name["ALM"], {"name": "ALM", "type": "Formative", "maxMarks": 40.0, "weight": 8.0})
        self.assertEqual(by_name["Home Assignment and Textbook"]["weight"], 6.0)
        self.assertEqual(by_name["Semester in Exam-I"], {"name": "Semester in Exam-I", "type": "Summative", "maxMarks": 50.0, "weight": 14.0})

    def test_parse_component_marks_ignores_negatives_and_erp_total(self):
        html = """
        <table>
          <tr>
            <th>Sno</th><th>Coursecode</th><th>Coursedesc</th><th>Remarks</th><th>Student Uniid</th>
            <th>1 A</th><th>1 B</th><th>1 C</th><th>2 A</th><th>2 B</th>
            <th>Max Marks</th><th>Weight</th><th>Total Marks</th><th>Weighted Marks</th>
          </tr>
          <tr>
            <td>1</td><td>24SC2006</td><td>OOPS</td><td></td><td>2400040231</td>
            <td>3</td><td>3</td><td>-2</td><td>5</td><td>2.5</td>
            <td>50</td><td>12</td><td>27</td><td>6.48</td>
          </tr>
        </table>
        """
        val = webapp.parse_component_marks(BeautifulSoup(html, "html.parser"))
        self.assertEqual(val, 13.5)

    def test_parse_component_marks_not_uploaded(self):
        html = "<div class='empty'>No results found.</div>"
        self.assertIsNone(webapp.parse_component_marks(BeautifulSoup(html, "html.parser")))

    def test_find_option_value_fallbacks(self):
        html = """
        <select>
          <option value="">Select</option>
          <option value="18">2026-27</option>
          <option value="1">ODD</option>
        </select>
        """
        sel = BeautifulSoup(html, "html.parser").find("select")
        self.assertEqual(webapp.find_option_value(sel, "2026-2027"), "18")
        self.assertEqual(webapp.find_option_value(sel, "Odd Sem"), "1")


if __name__ == "__main__":
    unittest.main()
