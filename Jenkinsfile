pipeline {
  agent any
  stages {
    stage('Build') {
      steps {
        sh 'docker build -t jioj/cb4:latest .'
      }
    }
  }
}