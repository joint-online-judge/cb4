import { AutoloadPage } from 'vj/misc/PageLoader';
import 'select2';
import 'select2/dist/css/select2.css';

const selectPage = new AutoloadPage('selectPage', () => {
  $('.select.select2').select2();
});

export default selectPage;
