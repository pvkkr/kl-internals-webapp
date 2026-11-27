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

    def test_parse_evaluation_plan_real_kl_erp_markup(self):
        html = """
        <table class="table table-bordered" border="1" cellpadding="1" width="100%;">
          <thead class="table-info">
            <tr>
              <th><b>Evaluation Type</b></th>
              <th><b>Evaluation Component</b></th>
              <th colspan="3"><b>Weightage/Marks</b></th>
              <th colspan="2"><b>Assessment Dates</b></th>
              <th colspan="2"> <b>Duration (Minutes)</b></th>
              <th colspan="2">CO1</th>
            </tr>
          </thead>
          <tbody>
            <tr>
              <td rowspan="2"><p><b>End Semester Summative EvaluationTotal= 40 %</b></p></td>
              <td rowspan="2"><b>Lab End Semester Exam</b></td>
              <td><p>Weightage</p></td>
              <td colspan="2 "><p>16</p></td>
              <td rowspan="2  " colspan="2"><p></p></td>
              <td rowspan="2  " colspan="2"><p>180</p></td>
              <td colspan="2"><p></p></td>
            </tr>
            <tr>
              <td><p>Max Marks</p></td>
              <td colspan="2 "><p>50</p></td>
              <td colspan="2"><p></p></td>
            </tr>
            <tr>
              <td rowspan="2"><p><b>In Semester Formative Evaluation Total= 24 %</b></p></td>
              <td rowspan="2"><b>ALM</b></td>
              <td><p>Weightage</p></td>
              <td colspan="2 "><p>8</p></td>
              <td rowspan="2  " colspan="2"><p></p></td>
              <td rowspan="2  " colspan="2"><p>100</p></td>
              <td colspan="2"><p>2</p></td>
            </tr>
            <tr>
              <td><p>Max Marks</p></td>
              <td colspan="2 "><p>100</p></td>
              <td colspan="2"><p>25</p></td>
            </tr>
          </tbody>
        </table>
        """
        plan = webapp.parse_evaluation_plan(BeautifulSoup(html, "html.parser"))
        self.assertEqual(len(plan), 1)
        self.assertEqual(plan[0], {"name": "ALM", "type": "Formative", "maxMarks": 100.0, "weight": 8.0})

    def test_parse_evaluation_plan_in_sem_short_labels(self):
        html = """
        <table>
          <tr><th>Evaluation Type</th><th>Evaluation Component</th><th colspan="3">Weightage/Marks</th></tr>
          <tr><td rowspan="2">End Sem Summative</td><td rowspan="2">End Exam</td><td>Weightage</td><td colspan="2">40</td></tr>
          <tr><td>Max Marks</td><td colspan="2">100</td></tr>
          <tr><td rowspan="2">In-Sem Formative</td><td rowspan="2">Quiz</td><td>Weightage</td><td colspan="2">10</td></tr>
          <tr><td>Max Marks</td><td colspan="2">20</td></tr>
          <tr><td rowspan="2">Summative Evaluation</td><td rowspan="2">Mid-1</td><td>Weightage</td><td colspan="2">20</td></tr>
          <tr><td>Max Marks</td><td colspan="2">50</td></tr>
        </table>
        """
        plan = webapp.parse_evaluation_plan(BeautifulSoup(html, "html.parser"))
        names = [c["name"] for c in plan]
        self.assertEqual(names, ["Quiz", "Mid-1"])

    def test_parse_evaluation_plan_nested_wrapper_table(self):
        html = """
        <table><tr><td>
          <table>
            <tr><th>Evaluation Type</th><th>Evaluation Component</th><th colspan="3">Weightage/Marks</th></tr>
            <tr><td rowspan="2">In Semester Formative</td><td rowspan="2">ALM</td><td>Weightage</td><td colspan="2">6 %</td></tr>
            <tr><td>Max Marks</td><td colspan="2">40</td></tr>
          </table>
        </td></tr></table>
        """
        plan = webapp.parse_evaluation_plan(BeautifulSoup(html, "html.parser"))
        self.assertEqual(plan, [{"name": "ALM", "type": "Formative", "maxMarks": 40.0, "weight": 6.0}])

    def test_parse_evaluation_plan_broken_rowspan_fallback(self):
        # End Semester has rowspan="6" (too large!), shifting subsequent rows in grid
        html = """
        <table>
          <tr><th>Evaluation Type</th><th>Evaluation Component</th><th colspan="3">Weightage/Marks</th></tr>
          <tr>
            <td rowspan="6">End Semester Summative Evaluation</td>
            <td rowspan="2">End Semester Exam</td>
            <td>Weightage</td><td colspan="2">24</td>
          </tr>
          <tr><td>Max Marks</td><td colspan="2">100</td></tr>
          <tr>
            <td rowspan="4">In Semester Formative Evaluation</td>
            <td rowspan="2">MOOCs Review</td>
            <td>Weightage</td><td colspan="2">5</td>
          </tr>
          <tr><td>Max Marks</td><td colspan="2">40</td></tr>
        </table>
        """
        plan = webapp.parse_evaluation_plan(BeautifulSoup(html, "html.parser"))
        self.assertEqual(plan, [{"name": "MOOCs Review", "type": "Formative", "maxMarks": 40.0, "weight": 5.0}])

    def test_parse_evaluation_plan_with_broken_earlier_table(self):
        broken_html = """
        <table><tr><td><b>Broken unclosed earlier table
        <table>
          <tr><th>Evaluation Type</th><th>Evaluation Component</th><th colspan="3">Weightage/Marks</th></tr>
          <tr><td rowspan="2">In Semester Formative Evaluation</td><td rowspan="2">ALM</td><td>Weightage</td><td colspan="2">6</td></tr>
          <tr><td>Max Marks</td><td colspan="2">40</td></tr>
        </table>
        """
        plan = webapp.parse_evaluation_plan(BeautifulSoup(broken_html, "html.parser"), raw_html=broken_html)
        self.assertEqual(plan, [{"name": "ALM", "type": "Formative", "maxMarks": 40.0, "weight": 6.0}])

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
